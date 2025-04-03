from dataclasses import dataclass
import os
from typing import List, Tuple, Generator as PyGenerator, Optional, Callable
import time
import queue
import threading
import platform
import torch
import torchaudio
from huggingface_hub import hf_hub_download
from models import Model, ModelArgs
from moshi.models import loaders
from tokenizers.processors import TemplateProcessing
from transformers import AutoTokenizer


@dataclass
class Segment:
    speaker: int
    text: str
    # (num_samples,), sample_rate = 24_000
    audio: torch.Tensor


def load_llama3_tokenizer():
    """
    https://github.com/huggingface/transformers/issues/22794#issuecomment-2092623992
    """
    tokenizer_name = "meta-llama/Llama-3.2-1B"
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_name)
    bos = tokenizer.bos_token
    eos = tokenizer.eos_token
    tokenizer._tokenizer.post_processor = TemplateProcessing(
        single=f"{bos}:0 $A:0 {eos}:0",
        pair=f"{bos}:0 $A:0 {eos}:0 {bos}:1 $B:1 {eos}:1",
        special_tokens=[(f"{bos}", tokenizer.bos_token_id), (f"{eos}", tokenizer.eos_token_id)],
    )

    return tokenizer


class Generator:
    def __init__(
        self,
        model: Model,
    ):
        self._model = model
        self._model.setup_caches(1)

        self._text_tokenizer = load_llama3_tokenizer()

        device = next(model.parameters()).device
        mimi_weight = hf_hub_download(loaders.DEFAULT_REPO, loaders.MIMI_NAME)
        mimi = loaders.get_mimi(mimi_weight, device=device)
        mimi.set_num_codebooks(32)
        self._audio_tokenizer = mimi

        self.sample_rate = mimi.sample_rate
        self.device = device
        
        # Buffer size for streaming (number of frames to collect before decoding)
        self._stream_buffer_size = 10

    def _tokenize_text_segment(self, text: str, speaker: int) -> Tuple[torch.Tensor, torch.Tensor]:
        frame_tokens = []
        frame_masks = []

        text_tokens = self._text_tokenizer.encode(f"[{speaker}]{text}")
        text_frame = torch.zeros(len(text_tokens), 33).long()
        text_frame_mask = torch.zeros(len(text_tokens), 33).bool()
        text_frame[:, -1] = torch.tensor(text_tokens)
        text_frame_mask[:, -1] = True

        frame_tokens.append(text_frame.to(self.device))
        frame_masks.append(text_frame_mask.to(self.device))

        return torch.cat(frame_tokens, dim=0), torch.cat(frame_masks, dim=0)

    def _tokenize_audio(self, audio: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        assert audio.ndim == 1, "Audio must be single channel"

        frame_tokens = []
        frame_masks = []

        # (K, T)
        audio = audio.to(self.device)
        audio_tokens = self._audio_tokenizer.encode(audio.unsqueeze(0).unsqueeze(0))[0]
        # add EOS frame
        eos_frame = torch.zeros(audio_tokens.size(0), 1).to(self.device)
        audio_tokens = torch.cat([audio_tokens, eos_frame], dim=1)

        audio_frame = torch.zeros(audio_tokens.size(1), 33).long().to(self.device)
        audio_frame_mask = torch.zeros(audio_tokens.size(1), 33).bool().to(self.device)
        audio_frame[:, :-1] = audio_tokens.transpose(0, 1)
        audio_frame_mask[:, :-1] = True

        frame_tokens.append(audio_frame)
        frame_masks.append(audio_frame_mask)

        return torch.cat(frame_tokens, dim=0), torch.cat(frame_masks, dim=0)

    def _tokenize_segment(self, segment: Segment) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Returns:
            (seq_len, 33), (seq_len, 33)
        """
        text_tokens, text_masks = self._tokenize_text_segment(segment.text, segment.speaker)
        audio_tokens, audio_masks = self._tokenize_audio(segment.audio)

        return torch.cat([text_tokens, audio_tokens], dim=0), torch.cat([text_masks, audio_masks], dim=0)
    
    def _decode_frames(self, frames):
        """Decode a batch of frames into audio with optimized memory handling"""
        if not frames:
            return torch.tensor([])
        
        audio = self._audio_tokenizer.decode(torch.stack(frames).permute(1, 2, 0)).squeeze(0).squeeze(0)

        return audio 

    @torch.inference_mode()
    def generate_stream(
        self,
        text: str,
        speaker: int,
        context: List[Segment],
        max_audio_length_ms: float = 90_000,
        temperature: float = 0.7,
        topk: int = 30,
        on_chunk_generated: Optional[Callable[[torch.Tensor], None]] = None,
    ) -> PyGenerator[torch.Tensor, None, None]:
        """
        Stream audio chunks as they're generated with extreme optimization for real-time performance.
        """
        # Enable CUDA optimizations
        if torch.cuda.is_available():
            torch.backends.cuda.matmul.allow_tf32 = True  # Allow TF32 for faster matrix multiplications
            torch.backends.cudnn.benchmark = True  # Optimize cudnn
            torch.cuda.empty_cache()
            torch.cuda.synchronize()
            
        self._model.reset_caches()

        max_generation_len = int(max_audio_length_ms / 80)
        
        tokens, tokens_mask = [], []
        
        context_start = time.time()
        if context:
            for segment in context:
                segment_tokens, segment_tokens_mask = self._tokenize_segment(segment)
                tokens.append(segment_tokens)
                tokens_mask.append(segment_tokens_mask)
        
        gen_segment_tokens, gen_segment_tokens_mask = self._tokenize_text_segment(text, speaker)
        tokens.append(gen_segment_tokens)
        tokens_mask.append(gen_segment_tokens_mask)
        
        context_time = time.time() - context_start
        if context_time > 0.1: 
            print(f"Context processing took {context_time:.3f}s")

        prompt_tokens = torch.cat(tokens, dim=0).long().to(self.device)
        prompt_tokens_mask = torch.cat(tokens_mask, dim=0).bool().to(self.device)

        curr_tokens = prompt_tokens.unsqueeze(0)
        curr_tokens_mask = prompt_tokens_mask.unsqueeze(0)
        curr_pos = torch.arange(0, prompt_tokens.size(0)).unsqueeze(0).long().to(self.device)

        max_seq_len = 2048
        max_context_len = max_seq_len - max_generation_len
        if curr_tokens.size(1) >= max_context_len:
            raise ValueError(
                f"Inputs too long, must be below max_seq_len - max_generation_len: {max_context_len}"
            )

        batch_size = 10 
        
        buffer_size = 30
        frame_buffer = []
        
        if torch.cuda.is_available():
            start_event = torch.cuda.Event(enable_timing=True)
            end_event = torch.cuda.Event(enable_timing=True)
            start_event.record()
        
        zeros_1_1 = torch.zeros(1, 1).long().to(self.device)
        zeros_mask_1_1 = torch.zeros(1, 1).bool().to(self.device)
        
        def update_tokens(sample):
            nonlocal curr_tokens, curr_tokens_mask, curr_pos
            ones = torch.ones_like(sample).bool()
            curr_tokens = torch.cat([sample, zeros_1_1], dim=1).unsqueeze(1)
            curr_tokens_mask = torch.cat([ones, zeros_mask_1_1], dim=1).unsqueeze(1)
            curr_pos = curr_pos[:, -1:] + 1
        
        with self._audio_tokenizer.streaming(1):
            i = 0
            generation_start = time.time()
            
            while i < max_generation_len:
                batch_end = min(i + batch_size, max_generation_len)
                batch_size_actual = batch_end - i
                
                batch_samples = []
                
                for j in range(batch_size_actual):
                    with torch.autocast(device_type=self.device.type, dtype=torch.bfloat16):
                        sample = self._model.generate_frame(curr_tokens, curr_tokens_mask, curr_pos, temperature, topk)
                    
                    if torch.all(sample == 0):
                        # EOS token, finish generation
                        break
                    
                    batch_samples.append(sample)
                    update_tokens(sample)  # Use optimized update function
                
                # Break if we hit EOS
                if not batch_samples:
                    break
                    
                # Add all samples to frame buffer
                frame_buffer.extend(batch_samples)
                i += len(batch_samples)
                
                # When buffer is full, decode and yield
                if len(frame_buffer) >= buffer_size:
                    frames_stacked = torch.stack(frame_buffer).permute(1, 2, 0)
                    
                    # Use fast synchronous path for audio decoding
                    audio_chunk = self._audio_tokenizer.decode(frames_stacked).squeeze(0).squeeze(0)
                    
                    frame_buffer = []
                    
                    if on_chunk_generated:
                        cpu_chunk = audio_chunk.cpu()
                        on_chunk_generated(cpu_chunk)
                        yield cpu_chunk
                    else:
                        yield audio_chunk.cpu()
                        
                # This prevents memory buildup if playback is much slower than generation
                if i >= 100 and (i % 100 == 0):
                    if torch.cuda.is_available():
                        # This ensures we don't queue up too many CUDA operations
                        torch.cuda.synchronize()
        
        if frame_buffer:
            frames_stacked = torch.stack(frame_buffer).permute(1, 2, 0)
            audio_chunk = self._audio_tokenizer.decode(frames_stacked).squeeze(0).squeeze(0)
            
            cpu_chunk = audio_chunk.cpu()
            if on_chunk_generated:
                on_chunk_generated(cpu_chunk)
            yield cpu_chunk
        
        # Report performance metrics
        if torch.cuda.is_available():
            end_event.record()
            torch.cuda.synchronize()
            gpu_time = start_event.elapsed_time(end_event) / 1000.0
            total_time = time.time() - generation_start
            
            frames_generated = i
            audio_seconds = frames_generated * 0.08  # 80ms per frame
            rtf = total_time / audio_seconds if audio_seconds > 0 else float('inf')
            
            print(f"GPU processing time: {gpu_time:.2f}s, Total time: {total_time:.2f}s")
            print(f"Generated {frames_generated} frames ({audio_seconds:.2f}s of audio)")
            print(f"Real-time factor: {rtf:.3f}x (target: <1.0)")


    @torch.inference_mode()
    def generate(
        self,
        text: str,
        speaker: int,
        context: List[Segment],
        max_audio_length_ms: float = 90_000,
        temperature: float = 0.7,
        topk: int = 30,
        stream: bool = False,
    ) -> torch.Tensor:
        """
        Generate audio for the given text with optimized speed.
        
        Args:
            text: Text to synthesize
            speaker: Speaker ID
            context: Context segments
            max_audio_length_ms: Maximum audio length in milliseconds
            temperature: Sampling temperature
            topk: Top-k sampling parameter
            stream: Whether to use streaming generation internally
            
        Returns:
            Generated audio tensor
        """
        if stream:
            # Use streaming implementation but combine all chunks at the end
            audio_chunks = []
            for chunk in self.generate_stream(text, speaker, context, max_audio_length_ms, temperature, topk):
                audio_chunks.append(chunk)
            
            if not audio_chunks:
                return torch.tensor([])
                
            # Concatenate all chunks
            return torch.cat(audio_chunks)
        
        # Non-streaming implementation
        # Clear the GPU memory cache to free up space
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            
        self._model.reset_caches()

        max_generation_len = int(max_audio_length_ms / 80)
        tokens, tokens_mask = [], []
        for segment in context:
            segment_tokens, segment_tokens_mask = self._tokenize_segment(segment)
            tokens.append(segment_tokens)
            tokens_mask.append(segment_tokens_mask)

        gen_segment_tokens, gen_segment_tokens_mask = self._tokenize_text_segment(text, speaker)
        tokens.append(gen_segment_tokens)
        tokens_mask.append(gen_segment_tokens_mask)

        prompt_tokens = torch.cat(tokens, dim=0).long().to(self.device)
        prompt_tokens_mask = torch.cat(tokens_mask, dim=0).bool().to(self.device)

        samples = []
        curr_tokens = prompt_tokens.unsqueeze(0)
        curr_tokens_mask = prompt_tokens_mask.unsqueeze(0)
        curr_pos = torch.arange(0, prompt_tokens.size(0)).unsqueeze(0).long().to(self.device)

        max_seq_len = 2048
        max_context_len = max_seq_len - max_generation_len
        if curr_tokens.size(1) >= max_context_len:
            raise ValueError(
                f"Inputs too long, must be below max_seq_len - max_generation_len: {max_context_len}"
            )

        # Use the streaming context manager for optimized decoding
        with self._audio_tokenizer.streaming(1):
            for i in range(max_generation_len):
                sample = self._model.generate_frame(curr_tokens, curr_tokens_mask, curr_pos, temperature, topk)
                if torch.all(sample == 0):
                    break  # eos

                samples.append(sample)

                curr_tokens = torch.cat([sample, torch.zeros(1, 1).long().to(self.device)], dim=1).unsqueeze(1)
                curr_tokens_mask = torch.cat(
                    [torch.ones_like(sample).bool(), torch.zeros(1, 1).bool().to(self.device)], dim=1
                ).unsqueeze(1)
                curr_pos = curr_pos[:, -1:] + 1

        if not samples:
            return torch.tensor([])
            
        audio = self._audio_tokenizer.decode(torch.stack(samples).permute(1, 2, 0)).squeeze(0).squeeze(0)
        return audio


class AudioStreamWriter:
    """
    Helper class for writing streaming audio to a file.
    """
    def __init__(self, filename, sample_rate):
        self.filename = filename
        self.sample_rate = sample_rate
        self.audio_chunks = []
        self.lock = threading.Lock()
        self.queue = queue.Queue()
        self.running = True
        
        # Start background writer thread
        self.writer_thread = threading.Thread(target=self._writer_worker, daemon=True)
        self.writer_thread.start()
        
    def _writer_worker(self):
        """Background thread that handles audio chunk processing"""
        buffer_chunks = []
        last_flush_time = time.time()
        
        while self.running or not self.queue.empty():
            try:
                # Get chunk with timeout to allow for regular checks
                chunk = self.queue.get(timeout=0.2)
                buffer_chunks.append(chunk)
                
                # Periodically flush the buffer to the main list
                current_time = time.time()
                if len(buffer_chunks) >= 10 or (current_time - last_flush_time > 2.0 and buffer_chunks):
                    with self.lock:
                        self.audio_chunks.extend(buffer_chunks)
                    buffer_chunks = []
                    last_flush_time = current_time
                    
            except queue.Empty:
                # If queue is empty but we have pending chunks, add them
                if buffer_chunks:
                    with self.lock:
                        self.audio_chunks.extend(buffer_chunks)
                    buffer_chunks = []
                    last_flush_time = time.time()
        
        # Final flush of any remaining chunks
        if buffer_chunks:
            with self.lock:
                self.audio_chunks.extend(buffer_chunks)
        
    def add_chunk(self, chunk):
        """Add an audio chunk to the buffer queue without blocking"""
        try:
            self.queue.put(chunk, timeout=0.1)
        except queue.Full:
            # If queue is full, add directly to avoid losing data
            with self.lock:
                self.audio_chunks.append(chunk)
    
    def write_file(self):
        """Write all collected audio chunks to file and clean up"""
        # Signal the background thread to stop
        self.running = False
        # Wait for the thread to finish with a timeout
        self.writer_thread.join(timeout=3.0)
        
        with self.lock:
            if not self.audio_chunks:
                return
                
            # Concatenate all chunks
            audio = torch.cat(self.audio_chunks)
            # Save to file
            torchaudio.save(self.filename, audio.unsqueeze(0).cpu(), self.sample_rate)

from safetensors.torch import load_file
import os
import torch
from models import Model, ModelArgs
from generator import Generator

def load_csm_1b_local(model_path: str, device: str = "cuda"):
    """
    Load the CSM-1B model from a local .safetensors checkpoint with extreme optimizations.
    """
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cuda.enable_flash_sdp(True)
    torch.backends.cudnn.benchmark = True
    torch.backends.cudnn.enabled = True
    
    print(f"Loading CSM-1B model from local checkpoint '{model_path}' with extreme optimizations...")

    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.synchronize()
    
    config = ModelArgs(
        backbone_flavor="llama-1B",
        decoder_flavor="llama-100M",
        text_vocab_size=128256,
        audio_vocab_size=2051,
        audio_num_codebooks=32,
    )

    model = Model(config)
    
    safetensor_path = os.path.join(model_path, "model.safetensors")
    state_dict = load_file(safetensor_path, device=device)
    model.load_state_dict(state_dict, strict=True)
    
    dtype = torch.bfloat16 if torch.cuda.is_available() and torch.cuda.is_bf16_supported() else torch.float16
    
    model = torch.compile(
        model,
        dynamic=True,
        fullgraph=True,
        backend='cudagraphs'
    )
    
    model.to(device=device, dtype=dtype)
    
    if torch.cuda.is_available():
        try:
            with torch.no_grad(), torch.cuda.amp.autocast(dtype=dtype):
                dummy_input = torch.zeros(1, 10, 33, device=device, dtype=torch.long)
                dummy_mask = torch.ones(1, 10, 33, device=device, dtype=torch.bool)
                dummy_pos = torch.arange(10, device=device).unsqueeze(0)
                _ = model(dummy_input, dummy_mask, dummy_pos)
                torch.cuda.synchronize()
        except Exception as e:
            print(f"Warm-up failed, but continuing anyway: {e}")

    print("Model compilation complete. Creating generator...")
    
    generator = Generator(model)
    
    generator._stream_buffer_size = 30 
    
    generator._tokenization_cache = {}
    
    original_tokenize_text = generator._tokenize_text_segment
    
    def patched_tokenize_text_segment(text, speaker):
        cache_key = f"{speaker}:{text}"
        if cache_key in generator._tokenization_cache:
            return generator._tokenization_cache[cache_key]
        
        result = original_tokenize_text(text, speaker)
        generator._tokenization_cache[cache_key] = result
        return result
    
    generator._tokenize_text_segment = patched_tokenize_text_segment
    
    return generator


def load_csm_1b(device: str = "cuda") -> Generator:
    """
    Load the CSM-1B model with extreme optimizations for real-time performance.
    """
    # Enable all CUDA optimizations
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cuda.enable_flash_sdp(True)
    torch.backends.cudnn.benchmark = True
    torch.backends.cudnn.enabled = True
    
    print("Loading CSM-1B model with extreme optimizations for real-time performance...")
    
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.synchronize()
    
    model = Model.from_pretrained("sesame/csm-1b")
    
    dtype = torch.bfloat16 if torch.cuda.is_available() and torch.cuda.is_bf16_supported() else torch.float16
    
    model = torch.compile(
        model,
        dynamic=True,
        fullgraph=True,
        backend='cudagraphs'
    )
    
    model.to(device=device, dtype=dtype)
    
    
    if torch.cuda.is_available():
        try:
            with torch.no_grad(), torch.cuda.amp.autocast(dtype=dtype):
                dummy_input = torch.zeros(1, 10, 33, device=device, dtype=torch.long)
                dummy_mask = torch.ones(1, 10, 33, device=device, dtype=torch.bool)
                dummy_pos = torch.arange(10, device=device).unsqueeze(0)
                _ = model(dummy_input, dummy_mask, dummy_pos)
                torch.cuda.synchronize()
        except Exception as e:
            print(f"Warm-up failed, but continuing anyway: {e}")
    
    print("Model compilation complete. Creating generator...")
    
    generator = Generator(model)
    
    generator._stream_buffer_size = 30
    
    
    generator._tokenization_cache = {}
    
    original_tokenize_text = generator._tokenize_text_segment
    
    def patched_tokenize_text_segment(text, speaker):
        cache_key = f"{speaker}:{text}"
        if cache_key in generator._tokenization_cache:
            return generator._tokenization_cache[cache_key]
        
        result = original_tokenize_text(text, speaker)
        generator._tokenization_cache[cache_key] = result
        return result
    
    generator._tokenize_text_segment = patched_tokenize_text_segment
    
    return generator


def generate_streaming_audio(
    generator: Generator,
    text: str,
    speaker: int,
    context: List[Segment],
    output_file: str,
    max_audio_length_ms: float = 90_000,
    temperature: float = 0.7,
    topk: int = 30,
    play_audio: bool = False,
):
    """
    Generate audio with ultra-optimized streaming output for real-time chat.
    """
    # Use direct file writing to minimize memory usage
    import os
    from threading import Lock
    
    # Set environment variables for better performance
    os.environ['OMP_NUM_THREADS'] = '1'  # Prevent OpenMP from using too many threads
    
    # Class to handle audio file writing with direct disk operations
    class FastAudioStreamWriter:
        def __init__(self, filename, sample_rate):
            self.filename = filename
            self.sample_rate = sample_rate
            self.temp_files = []
            self.chunk_count = 0
            self.lock = Lock()
            self.total_samples = 0
            
            # Create temp directory if needed
            import tempfile
            self.temp_dir = tempfile.mkdtemp()
            
        def add_chunk(self, chunk):
            """Write chunk directly to disk to minimize memory usage"""
            with self.lock:
                # Save chunk to temporary file
                temp_file = os.path.join(self.temp_dir, f"chunk_{self.chunk_count}.wav")
                torchaudio.save(temp_file, chunk.unsqueeze(0).cpu(), self.sample_rate)
                self.temp_files.append(temp_file)
                self.total_samples += chunk.size(0)
                self.chunk_count += 1
        
        def write_file(self):
            """Combine all chunk files into final output"""
            if not self.temp_files:
                return
                
            import subprocess
            
            # Use ffmpeg for fast concatenation if available
            try:
                with open(os.path.join(self.temp_dir, "file_list.txt"), "w") as f:
                    for temp_file in self.temp_files:
                        f.write(f"file '{temp_file}'\n")
                
                subprocess.run([
                    "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                    "-i", os.path.join(self.temp_dir, "file_list.txt"),
                    "-c", "copy", self.filename
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                # Clean up temp files
                for temp_file in self.temp_files:
                    try:
                        os.remove(temp_file)
                    except:
                        pass
                os.remove(os.path.join(self.temp_dir, "file_list.txt"))
                os.rmdir(self.temp_dir)
                    
            except (subprocess.SubprocessError, FileNotFoundError):
                # Fall back to manual concatenation if ffmpeg not available
                import numpy as np
                import wave
                
                with wave.open(self.filename, 'wb') as outfile:
                    outfile.setnchannels(1)
                    outfile.setsampwidth(2)  # 16-bit audio
                    outfile.setframerate(self.sample_rate)
                    
                    for temp_file in self.temp_files:
                        with wave.open(temp_file, 'rb') as infile:
                            outfile.writeframes(infile.readframes(infile.getnframes()))
                        
                        # Clean up temp file
                        try:
                            os.remove(temp_file)
                        except:
                            pass
                            
                    os.rmdir(self.temp_dir)
    
    writer = FastAudioStreamWriter(output_file, generator.sample_rate)
    
    # Use ring buffer for audio playback to decouple generation from playback
    import collections
    audio_ring_buffer = collections.deque(maxlen=50)  # Larger buffer to prevent underruns
    stop_event = threading.Event()
    
    if play_audio:
        try:
            import sounddevice as sd
            import numpy as np
            
            class AudioPlayer:
                def __init__(self, sample_rate):
                    self.sample_rate = sample_rate
                    self.buffer = np.zeros(0)
                    self.lock = threading.Lock()
                    self.stream = None
                    self.active = False
                    # Add extra padding at the end to ensure all audio is played
                    self.eos_padding = 0.5  # 500ms of silence at the end
                    self.done_playing = threading.Event()
                    
                def add_audio(self, chunk):
                    """Add audio to playback buffer"""
                    np_chunk = chunk.numpy()
                    with self.lock:
                        if self.buffer.size == 0:
                            self.buffer = np_chunk
                        else:
                            self.buffer = np.concatenate([self.buffer, np_chunk])
                
                def start(self):
                    """Start audio playback"""
                    self.active = True
                    self.done_playing.clear()
                    # Use a larger blocksize for better performance but not too large to avoid cutting off end
                    blocksize = 2048
                    
                    self.stream = sd.OutputStream(
                        samplerate=self.sample_rate,
                        channels=1,
                        callback=self._audio_callback,
                        finished_callback=self._stream_finished,
                        blocksize=blocksize
                    )
                    self.stream.start()
                
                def _audio_callback(self, outdata, frames, time, status):
                    """Callback to fill audio buffer"""
                    with self.lock:
                        if self.buffer.size > 0:
                            if self.buffer.size >= frames:
                                outdata[:] = self.buffer[:frames].reshape(-1, 1)
                                self.buffer = self.buffer[frames:]
                            else:
                                # Fill available data and zero-pad the rest
                                outdata[:self.buffer.size] = self.buffer.reshape(-1, 1)
                                outdata[self.buffer.size:] = 0
                                self.buffer = np.zeros(0)
                        else:
                            outdata[:] = 0
                
                def _stream_finished(self):
                    """Handle stream completion"""
                    with self.lock:
                        # Only mark as inactive if buffer is truly empty
                        if self.buffer.size == 0:
                            self.active = False
                            self.done_playing.set()
                
                def add_end_padding(self):
                    """Add silence padding at the end to ensure all audio is played"""
                    with self.lock:
                        # Add half a second of silence (zeros) to ensure the last utterance plays fully
                        padding_samples = int(self.sample_rate * self.eos_padding)
                        padding = np.zeros(padding_samples)
                        
                        if self.buffer.size == 0:
                            self.buffer = padding
                        else:
                            self.buffer = np.concatenate([self.buffer, padding])
                
                def stop(self, wait=True):
                    """Stop playback"""
                    self.add_end_padding()  # Add padding before stopping
                    
                    if wait and self.active:
                        # Wait for buffer to be empty before stopping
                        try:
                            # Wait with timeout to prevent hanging
                            self.done_playing.wait(timeout=5.0)
                        except:
                            pass
                    
                    if self.stream:
                        self.stream.stop()
                        self.stream.close()
                    
                    self.active = False
            
            # Create and start audio player
            player = AudioPlayer(generator.sample_rate)
            player.start()
            
            def audio_worker():
                """Worker thread to feed audio to player"""
                buffer_underrun_count = 0
                
                while not stop_event.is_set() or audio_ring_buffer:
                    if audio_ring_buffer:
                        chunk = audio_ring_buffer.popleft()
                        player.add_audio(chunk)
                        buffer_underrun_count = 0
                    else:
                        # Short sleep if buffer is empty
                        time.sleep(0.01)
                        buffer_underrun_count += 1
                        
                        # If we've reached the end of generation (stop event is set)
                        # and we've had buffer underruns for a while, add end padding
                        if stop_event.is_set() and buffer_underrun_count > 50:  # ~500ms of checking
                            player.add_end_padding()
                            buffer_underrun_count = 0
                
                # Make sure we wait for all audio to finish playing
                player.stop(wait=True)
            
            # Start audio worker thread
            player_thread = threading.Thread(target=audio_worker, daemon=True)
            player_thread.start()
            
        except ImportError:
            print("sounddevice library not found. Install with 'pip install sounddevice' to enable real-time playback.")
            play_audio = False
    
    # Define callback for handling generated chunks
    def on_chunk_generated(chunk):
        writer.add_chunk(chunk)
        if play_audio:
            audio_ring_buffer.append(chunk)
    
    # Pre-warm GPU and ensure minimal background activity
    if torch.cuda.is_available():
        # Run a small warm-up computation
        dummy_tensor = torch.ones(1, 1, device=generator.device)
        for _ in range(10):
            _ = dummy_tensor * 2.0
        torch.cuda.synchronize()
    
    print("Generating audio with streaming...")
    start_time = time.time()
    
    # Try to set high process priority if possible
    try:
        import psutil
        process = psutil.Process()
        process.nice(psutil.HIGH_PRIORITY_CLASS if platform.system() == 'Windows' else -10)
    except (ImportError, PermissionError):
        pass
    
    # Generate audio chunks
    chunk_count = 0
    for _ in generator.generate_stream(
        text=text,
        speaker=speaker,
        context=context,
        max_audio_length_ms=max_audio_length_ms,
        temperature=temperature,
        topk=topk,
        on_chunk_generated=on_chunk_generated
    ):
        chunk_count += 1
        # Minimize logging to reduce overhead
        if chunk_count % 10 == 0:
            elapsed = time.time() - start_time
            print(f"Generated {chunk_count} chunks in {elapsed:.2f}s ({chunk_count / elapsed:.1f} chunks/sec)")
    
    # Signal audio worker that generation is complete
    stop_event.set()
    
    # Write final file
    writer.write_file()
    
    # Wait for audio playback to complete
    if play_audio:
        print("Waiting for audio playback to complete...")
        try:
            player_thread.join(timeout=10.0)  # Longer timeout to ensure audio completes
        except:
            pass
    
    # Calculate performance metrics
    elapsed_time = time.time() - start_time
    audio_duration_seconds = chunk_count * generator._stream_buffer_size * 80 / 1000.0
    rtf = elapsed_time / audio_duration_seconds if audio_duration_seconds > 0 else float('inf')
    
    print(f"Audio generation completed in {elapsed_time:.2f} seconds")
    print(f"Generated {audio_duration_seconds:.2f} seconds of audio")
    print(f"Real-time factor (RTF): {rtf:.3f}x (target: <1.0)")