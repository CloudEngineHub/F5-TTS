import gc
import json
import os
import platform
import queue
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from glob import glob
from importlib.resources import files

import click
import gradio as gr
import librosa
import numpy as np
import psutil
import torch
import torchaudio
from cached_path import cached_path
from datasets import Dataset as Dataset_
from datasets.arrow_writer import ArrowWriter
from safetensors.torch import load_file, save_file
from scipy.io import wavfile

from f5_tts.api import F5TTS
from f5_tts.infer.utils_infer import transcribe
from f5_tts.model.utils import convert_char_to_pinyin


training_process = None
system = platform.system()
python_executable = sys.executable or "python"
tts_api = None
last_checkpoint = ""
last_device = ""
last_ema = None


path_data = str(files("f5_tts").joinpath("../../data"))
path_project_ckpts = str(files("f5_tts").joinpath("../../ckpts"))
file_train = str(files("f5_tts").joinpath("train/finetune_cli.py"))

device = (
    "cuda"
    if torch.cuda.is_available()
    else "xpu"
    if torch.xpu.is_available()
    else "mps"
    if torch.backends.mps.is_available()
    else "cpu"
)


# Save settings from a JSON file
def save_settings(
    project_name,
    exp_name,
    learning_rate,
    batch_size_per_gpu,
    batch_size_type,
    max_samples,
    grad_accumulation_steps,
    max_grad_norm,
    epochs,
    num_warmup_updates,
    save_per_updates,
    keep_last_n_checkpoints,
    last_per_updates,
    finetune,
    file_checkpoint_train,
    tokenizer_type,
    tokenizer_file,
    mixed_precision,
    logger,
    ch_8bit_adam,
):
    path_project = os.path.join(path_project_ckpts, project_name)
    os.makedirs(path_project, exist_ok=True)
    file_setting = os.path.join(path_project, "setting.json")

    settings = {
        "exp_name": exp_name,
        "learning_rate": learning_rate,
        "batch_size_per_gpu": batch_size_per_gpu,
        "batch_size_type": batch_size_type,
        "max_samples": max_samples,
        "grad_accumulation_steps": grad_accumulation_steps,
        "max_grad_norm": max_grad_norm,
        "epochs": epochs,
        "num_warmup_updates": num_warmup_updates,
        "save_per_updates": save_per_updates,
        "keep_last_n_checkpoints": keep_last_n_checkpoints,
        "last_per_updates": last_per_updates,
        "finetune": finetune,
        "file_checkpoint_train": file_checkpoint_train,
        "tokenizer_type": tokenizer_type,
        "tokenizer_file": tokenizer_file,
        "mixed_precision": mixed_precision,
        "logger": logger,
        "bnb_optimizer": ch_8bit_adam,
    }
    with open(file_setting, "w") as f:
        json.dump(settings, f, indent=4)
    return "Settings saved!"


# Load settings from a JSON file
def load_settings(project_name):
    project_name = project_name.replace("_pinyin", "").replace("_char", "")
    path_project = os.path.join(path_project_ckpts, project_name)
    file_setting = os.path.join(path_project, "setting.json")

    # Default settings
    default_settings = {
        "exp_name": "F5TTS_v1_Base",
        "learning_rate": 1e-5,
        "batch_size_per_gpu": 3200,
        "batch_size_type": "frame",
        "max_samples": 64,
        "grad_accumulation_steps": 1,
        "max_grad_norm": 1.0,
        "epochs": 100,
        "num_warmup_updates": 100,
        "save_per_updates": 500,
        "keep_last_n_checkpoints": -1,
        "last_per_updates": 100,
        "finetune": True,
        "file_checkpoint_train": "",
        "tokenizer_type": "pinyin",
        "tokenizer_file": "",
        "mixed_precision": "fp16",
        "logger": "none",
        "bnb_optimizer": False,
    }
    if device == "mps":
        default_settings["mixed_precision"] = "none"

    # Load settings from file if it exists
    if os.path.isfile(file_setting):
        with open(file_setting, "r") as f:
            file_settings = json.load(f)
        default_settings.update(file_settings)

    # Return as a tuple in the correct order
    return (
        default_settings["exp_name"],
        default_settings["learning_rate"],
        default_settings["batch_size_per_gpu"],
        default_settings["batch_size_type"],
        default_settings["max_samples"],
        default_settings["grad_accumulation_steps"],
        default_settings["max_grad_norm"],
        default_settings["epochs"],
        default_settings["num_warmup_updates"],
        default_settings["save_per_updates"],
        default_settings["keep_last_n_checkpoints"],
        default_settings["last_per_updates"],
        default_settings["finetune"],
        default_settings["file_checkpoint_train"],
        default_settings["tokenizer_type"],
        default_settings["tokenizer_file"],
        default_settings["mixed_precision"],
        default_settings["logger"],
        default_settings["bnb_optimizer"],
    )


# Load metadata
def get_audio_duration(audio_path):
    """Calculate the duration mono of an audio file."""
    audio, sample_rate = torchaudio.load(audio_path)
    return audio.shape[1] / sample_rate


class Slicer:  # https://github.com/RVC-Boss/GPT-SoVITS/blob/main/tools/slicer2.py
    def __init__(
        self,
        sr: int,
        threshold: float = -40.0,
        min_length: int = 20000,  # 20 seconds
        min_interval: int = 300,
        hop_size: int = 20,
        max_sil_kept: int = 2000,
    ):
        if not min_length >= min_interval >= hop_size:
            raise ValueError("The following condition must be satisfied: min_length >= min_interval >= hop_size")
        if not max_sil_kept >= hop_size:
            raise ValueError("The following condition must be satisfied: max_sil_kept >= hop_size")
        min_interval = sr * min_interval / 1000
        self.threshold = 10 ** (threshold / 20.0)
        self.hop_size = round(sr * hop_size / 1000)
        self.win_size = min(round(min_interval), 4 * self.hop_size)
        self.min_length = round(sr * min_length / 1000 / self.hop_size)
        self.min_interval = round(min_interval / self.hop_size)
        self.max_sil_kept = round(sr * max_sil_kept / 1000 / self.hop_size)

    def _apply_slice(self, waveform, begin, end):
        if len(waveform.shape) > 1:
            return waveform[:, begin * self.hop_size : min(waveform.shape[1], end * self.hop_size)]
        else:
            return waveform[begin * self.hop_size : min(waveform.shape[0], end * self.hop_size)]

    # @timeit
    def slice(self, waveform):
        if len(waveform.shape) > 1:
            samples = waveform.mean(axis=0)
        else:
            samples = waveform
        if samples.shape[0] <= self.min_length:
            return [waveform]
        rms_list = librosa.feature.rms(y=samples, frame_length=self.win_size, hop_length=self.hop_size).squeeze(0)
        sil_tags = []
        silence_start = None
        clip_start = 0
        for i, rms in enumerate(rms_list):
            # Keep looping while frame is silent.
            if rms < self.threshold:
                # Record start of silent frames.
                if silence_start is None:
                    silence_start = i
                continue
            # Keep looping while frame is not silent and silence start has not been recorded.
            if silence_start is None:
                continue
            # Clear recorded silence start if interval is not enough or clip is too short
            is_leading_silence = silence_start == 0 and i > self.max_sil_kept
            need_slice_middle = i - silence_start >= self.min_interval and i - clip_start >= self.min_length
            if not is_leading_silence and not need_slice_middle:
                silence_start = None
                continue
            # Need slicing. Record the range of silent frames to be removed.
            if i - silence_start <= self.max_sil_kept:
                pos = rms_list[silence_start : i + 1].argmin() + silence_start
                if silence_start == 0:
                    sil_tags.append((0, pos))
                else:
                    sil_tags.append((pos, pos))
                clip_start = pos
            elif i - silence_start <= self.max_sil_kept * 2:
                pos = rms_list[i - self.max_sil_kept : silence_start + self.max_sil_kept + 1].argmin()
                pos += i - self.max_sil_kept
                pos_l = rms_list[silence_start : silence_start + self.max_sil_kept + 1].argmin() + silence_start
                pos_r = rms_list[i - self.max_sil_kept : i + 1].argmin() + i - self.max_sil_kept
                if silence_start == 0:
                    sil_tags.append((0, pos_r))
                    clip_start = pos_r
                else:
                    sil_tags.append((min(pos_l, pos), max(pos_r, pos)))
                    clip_start = max(pos_r, pos)
            else:
                pos_l = rms_list[silence_start : silence_start + self.max_sil_kept + 1].argmin() + silence_start
                pos_r = rms_list[i - self.max_sil_kept : i + 1].argmin() + i - self.max_sil_kept
                if silence_start == 0:
                    sil_tags.append((0, pos_r))
                else:
                    sil_tags.append((pos_l, pos_r))
                clip_start = pos_r
            silence_start = None
        # Deal with trailing silence.
        total_frames = rms_list.shape[0]
        if silence_start is not None and total_frames - silence_start >= self.min_interval:
            silence_end = min(total_frames, silence_start + self.max_sil_kept)
            pos = rms_list[silence_start : silence_end + 1].argmin() + silence_start
            sil_tags.append((pos, total_frames + 1))
        # Apply and return slices: [chunk, start, end]
        if len(sil_tags) == 0:
            return [[waveform, 0, int(total_frames * self.hop_size)]]
        else:
            chunks = []
            if sil_tags[0][0] > 0:
                chunks.append([self._apply_slice(waveform, 0, sil_tags[0][0]), 0, int(sil_tags[0][0] * self.hop_size)])
            for i in range(len(sil_tags) - 1):
                chunks.append(
                    [
                        self._apply_slice(waveform, sil_tags[i][1], sil_tags[i + 1][0]),
                        int(sil_tags[i][1] * self.hop_size),
                        int(sil_tags[i + 1][0] * self.hop_size),
                    ]
                )
            if sil_tags[-1][1] < total_frames:
                chunks.append(
                    [
                        self._apply_slice(waveform, sil_tags[-1][1], total_frames),
                        int(sil_tags[-1][1] * self.hop_size),
                        int(total_frames * self.hop_size),
                    ]
                )
            return chunks


# terminal
def terminate_process_tree(pid, including_parent=True):
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        # Process already terminated
        return

    children = parent.children(recursive=True)
    for child in children:
        try:
            os.kill(child.pid, signal.SIGTERM)  # or signal.SIGKILL
        except OSError:
            pass
    if including_parent:
        try:
            os.kill(parent.pid, signal.SIGTERM)  # or signal.SIGKILL
        except OSError:
            pass


def terminate_process(pid):
    if system == "Windows":
        cmd = f"taskkill /t /f /pid {pid}"
        os.system(cmd)
    else:
        terminate_process_tree(pid)


def start_training(
    dataset_name,
    exp_name,
    learning_rate,
    batch_size_per_gpu,
    batch_size_type,
    max_samples,
    grad_accumulation_steps,
    max_grad_norm,
    epochs,
    num_warmup_updates,
    save_per_updates,
    keep_last_n_checkpoints,
    last_per_updates,
    finetune,
    file_checkpoint_train,
    tokenizer_type,
    tokenizer_file,
    mixed_precision,
    stream,
    logger,
    ch_8bit_adam,
):
    global training_process, tts_api, stop_signal

    if tts_api is not None:
        if tts_api is not None:
            del tts_api

        gc.collect()
        torch.cuda.empty_cache()
        tts_api = None

    path_project = os.path.join(path_data, dataset_name)

    if not os.path.isdir(path_project):
        yield (
            f"There is not project with name {dataset_name}",
            gr.update(interactive=True),
            gr.update(interactive=False),
        )
        return

    file_raw = os.path.join(path_project, "raw.arrow")
    if not os.path.isfile(file_raw):
        yield f"There is no file {file_raw}", gr.update(interactive=True), gr.update(interactive=False)
        return

    # Check if a training process is already running
    if training_process is not None:
        return "Train run already!", gr.update(interactive=False), gr.update(interactive=True)

    yield "start train", gr.update(interactive=False), gr.update(interactive=False)

    # Command to run the training script with the specified arguments

    if tokenizer_file == "":
        if dataset_name.endswith("_pinyin"):
            tokenizer_type = "pinyin"
        elif dataset_name.endswith("_char"):
            tokenizer_type = "char"
    else:
        tokenizer_type = "custom"

    dataset_name = dataset_name.replace("_pinyin", "").replace("_char", "")

    if mixed_precision != "none":
        fp16 = f"--mixed_precision={mixed_precision}"
    else:
        fp16 = ""

    cmd = (
        f'accelerate launch {fp16} "{file_train}" --exp_name {exp_name}'
        f" --learning_rate {learning_rate}"
        f" --batch_size_per_gpu {batch_size_per_gpu}"
        f" --batch_size_type {batch_size_type}"
        f" --max_samples {max_samples}"
        f" --grad_accumulation_steps {grad_accumulation_steps}"
        f" --max_grad_norm {max_grad_norm}"
        f" --epochs {epochs}"
        f" --num_warmup_updates {num_warmup_updates}"
        f" --save_per_updates {save_per_updates}"
        f" --keep_last_n_checkpoints {keep_last_n_checkpoints}"
        f" --last_per_updates {last_per_updates}"
        f" --dataset_name {dataset_name}"
    )

    if finetune:
        cmd += " --finetune"

    if file_checkpoint_train != "":
        cmd += f' --pretrain "{file_checkpoint_train}"'

    if tokenizer_file != "":
        cmd += f" --tokenizer_path {tokenizer_file}"

    cmd += f" --tokenizer {tokenizer_type}"

    if logger != "none":
        cmd += f" --logger {logger}"

    cmd += " --log_samples"

    if ch_8bit_adam:
        cmd += " --bnb_optimizer"

    print("run command : \n" + cmd + "\n")

    save_settings(
        dataset_name,
        exp_name,
        learning_rate,
        batch_size_per_gpu,
        batch_size_type,
        max_samples,
        grad_accumulation_steps,
        max_grad_norm,
        epochs,
        num_warmup_updates,
        save_per_updates,
        keep_last_n_checkpoints,
        last_per_updates,
        finetune,
        file_checkpoint_train,
        tokenizer_type,
        tokenizer_file,
        mixed_precision,
        logger,
        ch_8bit_adam,
    )

    try:
        if not stream:
            # Start the training process
            training_process = subprocess.Popen(cmd, shell=True)

            time.sleep(5)
            yield "train start", gr.update(interactive=False), gr.update(interactive=True)

            # Wait for the training process to finish
            training_process.wait()
        else:

            def stream_output(pipe, output_queue):
                try:
                    for line in iter(pipe.readline, ""):
                        output_queue.put(line)
                except Exception as e:
                    output_queue.put(f"Error reading pipe: {str(e)}")
                finally:
                    pipe.close()

            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"

            training_process = subprocess.Popen(
                cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, env=env
            )
            yield "Training started ...", gr.update(interactive=False), gr.update(interactive=True)

            stdout_queue = queue.Queue()
            stderr_queue = queue.Queue()

            stdout_thread = threading.Thread(target=stream_output, args=(training_process.stdout, stdout_queue))
            stderr_thread = threading.Thread(target=stream_output, args=(training_process.stderr, stderr_queue))
            stdout_thread.daemon = True
            stderr_thread.daemon = True
            stdout_thread.start()
            stderr_thread.start()
            stop_signal = False
            while True:
                if stop_signal:
                    training_process.terminate()
                    time.sleep(0.5)
                    if training_process.poll() is None:
                        training_process.kill()
                    yield "Training stopped by user.", gr.update(interactive=True), gr.update(interactive=False)
                    break

                process_status = training_process.poll()

                # Handle stdout
                try:
                    while True:
                        output = stdout_queue.get_nowait()
                        print(output, end="")
                        match = re.search(
                            r"Epoch (\d+)/(\d+):\s+(\d+)%\|.*\[(\d+:\d+)<.*?loss=(\d+\.\d+), update=(\d+)", output
                        )
                        if match:
                            current_epoch = match.group(1)
                            total_epochs = match.group(2)
                            percent_complete = match.group(3)
                            elapsed_time = match.group(4)
                            loss = match.group(5)
                            current_update = match.group(6)
                            message = (
                                f"Epoch: {current_epoch}/{total_epochs}, "
                                f"Progress: {percent_complete}%, "
                                f"Elapsed Time: {elapsed_time}, "
                                f"Loss: {loss}, "
                                f"Update: {current_update}"
                            )
                            yield message, gr.update(interactive=False), gr.update(interactive=True)
                        elif output.strip():
                            yield output, gr.update(interactive=False), gr.update(interactive=True)
                except queue.Empty:
                    pass

                # Handle stderr
                try:
                    while True:
                        error_output = stderr_queue.get_nowait()
                        print(error_output, end="")
                        if error_output.strip():
                            yield f"{error_output.strip()}", gr.update(interactive=False), gr.update(interactive=True)
                except queue.Empty:
                    pass

                if process_status is not None and stdout_queue.empty() and stderr_queue.empty():
                    if process_status != 0:
                        yield (
                            f"Process crashed with exit code {process_status}!",
                            gr.update(interactive=False),
                            gr.update(interactive=True),
                        )
                    else:
                        yield (
                            "Training complete or paused ...",
                            gr.update(interactive=False),
                            gr.update(interactive=True),
                        )
                    break

                # Small sleep to prevent CPU thrashing
                time.sleep(0.1)

            # Clean up
            training_process.stdout.close()
            training_process.stderr.close()
            training_process.wait()

        time.sleep(1)

        if training_process is None:
            text_info = "Train stopped !"
        else:
            text_info = "Train complete at end !"

    except Exception as e:  # Catch all exceptions
        # Ensure that we reset the training process variable in case of an error
        text_info = f"An error occurred: {str(e)}"

    training_process = None

    yield text_info, gr.update(interactive=True), gr.update(interactive=False)


def stop_training():
    global training_process, stop_signal

    if training_process is None:
        return "Train not running !", gr.update(interactive=True), gr.update(interactive=False)
    terminate_process_tree(training_process.pid)
    # training_process = None
    stop_signal = True
    return "Train stopped !", gr.update(interactive=True), gr.update(interactive=False)


def get_list_projects():
    project_list = []
    for folder in os.listdir(path_data):
        path_folder = os.path.join(path_data, folder)
        if not os.path.isdir(path_folder):
            continue
        folder = folder.lower()
        if folder == "emilia_zh_en_pinyin":
            continue
        project_list.append(folder)

    projects_selelect = None if not project_list else project_list[-1]

    return project_list, projects_selelect


def create_data_project(name, tokenizer_type):
    name += "_" + tokenizer_type
    os.makedirs(os.path.join(path_data, name), exist_ok=True)
    os.makedirs(os.path.join(path_data, name, "dataset"), exist_ok=True)
    project_list, projects_selelect = get_list_projects()
    return gr.update(choices=project_list, value=name)


def transcribe_all(name_project, audio_files, language, user=False, progress=gr.Progress()):
    path_project = os.path.join(path_data, name_project)
    path_dataset = os.path.join(path_project, "dataset")
    path_project_wavs = os.path.join(path_project, "wavs")
    file_metadata = os.path.join(path_project, "metadata.csv")

    if not user:
        if audio_files is None:
            return "You need to load an audio file."

    if os.path.isdir(path_project_wavs):
        shutil.rmtree(path_project_wavs)

    if os.path.isfile(file_metadata):
        os.remove(file_metadata)

    os.makedirs(path_project_wavs, exist_ok=True)

    if user:
        file_audios = [
            file
            for format in ("*.wav", "*.ogg", "*.opus", "*.mp3", "*.flac")
            for file in glob(os.path.join(path_dataset, format))
        ]
        if file_audios == []:
            return "No audio file was found in the dataset."
    else:
        file_audios = audio_files

    alpha = 0.5
    _max = 1.0
    slicer = Slicer(24000)

    num = 0
    error_num = 0
    data = ""
    for file_audio in progress.tqdm(file_audios, desc="transcribe files", total=len((file_audios))):
        audio, _ = librosa.load(file_audio, sr=24000, mono=True)

        list_slicer = slicer.slice(audio)
        for chunk, start, end in progress.tqdm(list_slicer, total=len(list_slicer), desc="slicer files"):
            name_segment = os.path.join(f"segment_{num}")
            file_segment = os.path.join(path_project_wavs, f"{name_segment}.wav")

            tmp_max = np.abs(chunk).max()
            if tmp_max > 1:
                chunk /= tmp_max
            chunk = (chunk / tmp_max * (_max * alpha)) + (1 - alpha) * chunk
            wavfile.write(file_segment, 24000, (chunk * 32767).astype(np.int16))

            try:
                text = transcribe(file_segment, language)
                text = text.strip()

                data += f"{name_segment}|{text}\n"

                num += 1
            except:  # noqa: E722
                error_num += 1

    with open(file_metadata, "w", encoding="utf-8-sig") as f:
        f.write(data)

    if error_num != []:
        error_text = f"\nerror files : {error_num}"
    else:
        error_text = ""

    return f"transcribe complete samples : {num}\npath : {path_project_wavs}{error_text}"


def format_seconds_to_hms(seconds):
    hours = int(seconds / 3600)
    minutes = int((seconds % 3600) / 60)
    seconds = seconds % 60
    return "{:02d}:{:02d}:{:02d}".format(hours, minutes, int(seconds))


def get_correct_audio_path(
    audio_input,
    base_path="wavs",
    supported_formats=("wav", "mp3", "aac", "flac", "m4a", "alac", "ogg", "aiff", "wma", "amr"),
):
    file_audio = None

    # Helper function to check if file has a supported extension
    def has_supported_extension(file_name):
        return any(file_name.endswith(f".{ext}") for ext in supported_formats)

    # Case 1: If it's a full path with a valid extension, use it directly
    if os.path.isabs(audio_input) and has_supported_extension(audio_input):
        file_audio = audio_input

    # Case 2: If it has a supported extension but is not a full path
    elif has_supported_extension(audio_input) and not os.path.isabs(audio_input):
        file_audio = os.path.join(base_path, audio_input)

    # Case 3: If only the name is given (no extension and not a full path)
    elif not has_supported_extension(audio_input) and not os.path.isabs(audio_input):
        for ext in supported_formats:
            potential_file = os.path.join(base_path, f"{audio_input}.{ext}")
            if os.path.exists(potential_file):
                file_audio = potential_file
                break
        else:
            file_audio = os.path.join(base_path, f"{audio_input}.{supported_formats[0]}")
    return file_audio


def create_metadata(name_project, ch_tokenizer, progress=gr.Progress()):
    path_project = os.path.join(path_data, name_project)
    path_project_wavs = os.path.join(path_project, "wavs")
    file_metadata = os.path.join(path_project, "metadata.csv")
    file_raw = os.path.join(path_project, "raw.arrow")
    file_duration = os.path.join(path_project, "duration.json")
    file_vocab = os.path.join(path_project, "vocab.txt")

    if not os.path.isfile(file_metadata):
        return "The file was not found in " + file_metadata, ""

    with open(file_metadata, "r", encoding="utf-8-sig") as f:
        data = f.read()

    audio_path_list = []
    text_list = []
    duration_list = []

    count = data.split("\n")
    lenght = 0
    result = []
    error_files = []
    text_vocab_set = set()
    for line in progress.tqdm(data.split("\n"), total=count):
        sp_line = line.split("|")
        if len(sp_line) != 2:
            continue
        name_audio, text = sp_line[:2]

        file_audio = get_correct_audio_path(name_audio, path_project_wavs)

        if not os.path.isfile(file_audio):
            error_files.append([file_audio, "error path"])
            continue

        try:
            duration = get_audio_duration(file_audio)
        except Exception as e:
            error_files.append([file_audio, "duration"])
            print(f"Error processing {file_audio}: {e}")
            continue

        if duration < 1 or duration > 30:
            if duration > 30:
                error_files.append([file_audio, "duration > 30 sec"])
            if duration < 1:
                error_files.append([file_audio, "duration < 1 sec "])
            continue
        if len(text) < 3:
            error_files.append([file_audio, "very short text length 3"])
            continue

        text = text.strip()
        text = convert_char_to_pinyin([text], polyphone=True)[0]

        audio_path_list.append(file_audio)
        duration_list.append(duration)
        text_list.append(text)

        result.append({"audio_path": file_audio, "text": text, "duration": duration})
        if ch_tokenizer:
            text_vocab_set.update(list(text))

        lenght += duration

    if duration_list == []:
        return f"Error: No audio files found in the specified path : {path_project_wavs}", ""

    min_second = round(min(duration_list), 2)
    max_second = round(max(duration_list), 2)

    with ArrowWriter(path=file_raw) as writer:
        for line in progress.tqdm(result, total=len(result), desc="prepare data"):
            writer.write(line)
        writer.finalize()

    with open(file_duration, "w") as f:
        json.dump({"duration": duration_list}, f, ensure_ascii=False)

    new_vocal = ""
    if not ch_tokenizer:
        if not os.path.isfile(file_vocab):
            file_vocab_finetune = os.path.join(path_data, "Emilia_ZH_EN_pinyin/vocab.txt")
            if not os.path.isfile(file_vocab_finetune):
                return "Error: Vocabulary file 'Emilia_ZH_EN_pinyin' not found!", ""
            shutil.copy2(file_vocab_finetune, file_vocab)

        with open(file_vocab, "r", encoding="utf-8-sig") as f:
            vocab_char_map = {}
            for i, char in enumerate(f):
                vocab_char_map[char[:-1]] = i
        vocab_size = len(vocab_char_map)

    else:
        with open(file_vocab, "w", encoding="utf-8-sig") as f:
            for vocab in sorted(text_vocab_set):
                f.write(vocab + "\n")
                new_vocal += vocab + "\n"
        vocab_size = len(text_vocab_set)

    if error_files != []:
        error_text = "\n".join([" = ".join(item) for item in error_files])
    else:
        error_text = ""

    return (
        f"prepare complete \nsamples : {len(text_list)}\ntime data : {format_seconds_to_hms(lenght)}\nmin sec : {min_second}\nmax sec : {max_second}\nfile_arrow : {file_raw}\nvocab : {vocab_size}\n{error_text}",
        new_vocal,
    )


def check_user(value):
    return gr.update(visible=not value), gr.update(visible=value)


def calculate_train(
    name_project,
    epochs,
    learning_rate,
    batch_size_per_gpu,
    batch_size_type,
    max_samples,
    num_warmup_updates,
    finetune,
):
    path_project = os.path.join(path_data, name_project)
    file_duration = os.path.join(path_project, "duration.json")

    hop_length = 256
    sampling_rate = 24000

    if not os.path.isfile(file_duration):
        return (
            epochs,
            learning_rate,
            batch_size_per_gpu,
            max_samples,
            num_warmup_updates,
            "project not found !",
        )

    with open(file_duration, "r") as file:
        data = json.load(file)

    duration_list = data["duration"]
    max_sample_length = max(duration_list) * sampling_rate / hop_length
    total_samples = len(duration_list)
    total_duration = sum(duration_list)

    if torch.cuda.is_available():
        gpu_count = torch.cuda.device_count()
        total_memory = 0
        for i in range(gpu_count):
            gpu_properties = torch.cuda.get_device_properties(i)
            total_memory += gpu_properties.total_memory / (1024**3)  # in GB
    elif torch.xpu.is_available():
        gpu_count = torch.xpu.device_count()
        total_memory = 0
        for i in range(gpu_count):
            gpu_properties = torch.xpu.get_device_properties(i)
            total_memory += gpu_properties.total_memory / (1024**3)
    elif torch.backends.mps.is_available():
        gpu_count = 1
        total_memory = psutil.virtual_memory().available / (1024**3)

    avg_gpu_memory = total_memory / gpu_count

    # rough estimate of batch size
    if batch_size_type == "frame":
        batch_size_per_gpu = max(int(38400 * (avg_gpu_memory - 5) / 75), int(max_sample_length))
    elif batch_size_type == "sample":
        batch_size_per_gpu = int(200 / (total_duration / total_samples))

    if total_samples < 64:
        max_samples = int(total_samples * 0.25)

    num_warmup_updates = max(num_warmup_updates, int(total_samples * 0.05))

    # take 1.2M updates as the maximum
    max_updates = 1200000

    if batch_size_type == "frame":
        mini_batch_duration = batch_size_per_gpu * gpu_count * hop_length / sampling_rate
        updates_per_epoch = total_duration / mini_batch_duration
    elif batch_size_type == "sample":
        updates_per_epoch = total_samples / batch_size_per_gpu / gpu_count

    epochs = int(max_updates / updates_per_epoch)

    if finetune:
        learning_rate = 1e-5
    else:
        learning_rate = 7.5e-5

    return (
        epochs,
        learning_rate,
        batch_size_per_gpu,
        max_samples,
        num_warmup_updates,
        total_samples,
    )


def prune_checkpoint(checkpoint_path: str, new_checkpoint_path: str, save_ema: bool, safetensors: bool) -> str:
    try:
        checkpoint = torch.load(checkpoint_path, weights_only=True)
        print("Original Checkpoint Keys:", checkpoint.keys())

        to_retain = "ema_model_state_dict" if save_ema else "model_state_dict"
        try:
            model_state_dict_to_retain = checkpoint[to_retain]
        except KeyError:
            return f"{to_retain} not found in the checkpoint."

        if safetensors:
            new_checkpoint_path = new_checkpoint_path.replace(".pt", ".safetensors")
            save_file(model_state_dict_to_retain, new_checkpoint_path)
        else:
            new_checkpoint_path = new_checkpoint_path.replace(".safetensors", ".pt")
            new_checkpoint = {"ema_model_state_dict": model_state_dict_to_retain}
            torch.save(new_checkpoint, new_checkpoint_path)

        return f"New checkpoint saved at: {new_checkpoint_path}"

    except Exception as e:
        return f"An error occurred: {e}"


def expand_model_embeddings(ckpt_path, new_ckpt_path, num_new_tokens=42):
    seed = 666
    random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

    if ckpt_path.endswith(".safetensors"):
        ckpt = load_file(ckpt_path, device="cpu")
        ckpt = {"ema_model_state_dict": ckpt}
    elif ckpt_path.endswith(".pt"):
        ckpt = torch.load(ckpt_path, map_location="cpu")

    ema_sd = ckpt.get("ema_model_state_dict", {})
    embed_key_ema = "ema_model.transformer.text_embed.text_embed.weight"
    old_embed_ema = ema_sd[embed_key_ema]

    vocab_old = old_embed_ema.size(0)
    embed_dim = old_embed_ema.size(1)
    vocab_new = vocab_old + num_new_tokens

    def expand_embeddings(old_embeddings):
        new_embeddings = torch.zeros((vocab_new, embed_dim))
        new_embeddings[:vocab_old] = old_embeddings
        new_embeddings[vocab_old:] = torch.randn((num_new_tokens, embed_dim))
        return new_embeddings

    ema_sd[embed_key_ema] = expand_embeddings(ema_sd[embed_key_ema])

    if new_ckpt_path.endswith(".safetensors"):
        save_file(ema_sd, new_ckpt_path)
    elif new_ckpt_path.endswith(".pt"):
        torch.save(ckpt, new_ckpt_path)

    return vocab_new


def vocab_count(text):
    return str(len(text.split(",")))


def vocab_extend(project_name, symbols, model_type):
    if symbols == "":
        return "Symbols empty!"

    name_project = project_name
    path_project = os.path.join(path_data, name_project)
    file_vocab_project = os.path.join(path_project, "vocab.txt")

    file_vocab = os.path.join(path_data, "Emilia_ZH_EN_pinyin/vocab.txt")
    if not os.path.isfile(file_vocab):
        return f"the file {file_vocab} not found !"

    symbols = symbols.split(",")
    if symbols == []:
        return "Symbols to extend not found."

    with open(file_vocab, "r", encoding="utf-8-sig") as f:
        data = f.read()
        vocab = data.split("\n")
    vocab_check = set(vocab)

    miss_symbols = []
    for item in symbols:
        item = item.replace(" ", "")
        if item in vocab_check:
            continue
        miss_symbols.append(item)

    if miss_symbols == []:
        return "Symbols are okay no need to extend."

    size_vocab = len(vocab)
    vocab.pop()
    for item in miss_symbols:
        vocab.append(item)

    vocab.append("")

    with open(file_vocab_project, "w", encoding="utf-8") as f:
        f.write("\n".join(vocab))

    if model_type == "F5TTS_v1_Base":
        ckpt_path = str(cached_path("hf://SWivid/F5-TTS/F5TTS_v1_Base/model_1250000.safetensors"))
    elif model_type == "F5TTS_Base":
        ckpt_path = str(cached_path("hf://SWivid/F5-TTS/F5TTS_Base/model_1200000.pt"))
    elif model_type == "E2TTS_Base":
        ckpt_path = str(cached_path("hf://SWivid/E2-TTS/E2TTS_Base/model_1200000.pt"))

    vocab_size_new = len(miss_symbols)

    dataset_name = name_project.replace("_pinyin", "").replace("_char", "")
    new_ckpt_path = os.path.join(path_project_ckpts, dataset_name)
    os.makedirs(new_ckpt_path, exist_ok=True)

    # Add pretrained_ prefix to model when copying for consistency with finetune_cli.py
    new_ckpt_file = os.path.join(new_ckpt_path, "pretrained_" + os.path.basename(ckpt_path))

    size = expand_model_embeddings(ckpt_path, new_ckpt_file, num_new_tokens=vocab_size_new)

    vocab_new = "\n".join(miss_symbols)
    return f"vocab old size : {size_vocab}\nvocab new size : {size}\nvocab add : {vocab_size_new}\nnew symbols :\n{vocab_new}"


def vocab_check(project_name, tokenizer_type):
    name_project = project_name
    path_project = os.path.join(path_data, name_project)

    file_metadata = os.path.join(path_project, "metadata.csv")

    file_vocab = os.path.join(path_data, "Emilia_ZH_EN_pinyin/vocab.txt")
    if not os.path.isfile(file_vocab):
        return f"the file {file_vocab} not found !", ""

    with open(file_vocab, "r", encoding="utf-8-sig") as f:
        data = f.read()
        vocab = data.split("\n")
        vocab = set(vocab)

    if not os.path.isfile(file_metadata):
        return f"the file {file_metadata} not found !", ""

    with open(file_metadata, "r", encoding="utf-8-sig") as f:
        data = f.read()

    miss_symbols = []
    miss_symbols_keep = {}
    for item in data.split("\n"):
        sp = item.split("|")
        if len(sp) != 2:
            continue

        text = sp[1].strip()
        if tokenizer_type == "pinyin":
            text = convert_char_to_pinyin([text], polyphone=True)[0]

        for t in text:
            if t not in vocab and t not in miss_symbols_keep:
                miss_symbols.append(t)
                miss_symbols_keep[t] = t

    if miss_symbols == []:
        vocab_miss = ""
        info = "You can train using your language !"
    else:
        vocab_miss = ",".join(miss_symbols)
        info = f"The following {len(miss_symbols)} symbols are missing in your language\n\n"

    return info, vocab_miss


def get_random_sample_prepare(project_name):
    name_project = project_name
    path_project = os.path.join(path_data, name_project)
    file_arrow = os.path.join(path_project, "raw.arrow")
    if not os.path.isfile(file_arrow):
        return "", None
    dataset = Dataset_.from_file(file_arrow)
    random_sample = dataset.shuffle(seed=random.randint(0, 1000)).select([0])
    text = "[" + " , ".join(["' " + t + " '" for t in random_sample["text"][0]]) + "]"
    audio_path = random_sample["audio_path"][0]
    return text, audio_path


def get_random_sample_transcribe(project_name):
    name_project = project_name
    path_project = os.path.join(path_data, name_project)
    file_metadata = os.path.join(path_project, "metadata.csv")
    if not os.path.isfile(file_metadata):
        return "", None

    data = ""
    with open(file_metadata, "r", encoding="utf-8-sig") as f:
        data = f.read()

    list_data = []
    for item in data.split("\n"):
        sp = item.split("|")
        if len(sp) != 2:
            continue

        # fixed audio when it is absolute
        file_audio = get_correct_audio_path(sp[0], os.path.join(path_project, "wavs"))
        list_data.append([file_audio, sp[1]])

    if list_data == []:
        return "", None

    random_item = random.choice(list_data)

    return random_item[1], random_item[0]


def get_random_sample_infer(project_name):
    text, audio = get_random_sample_transcribe(project_name)
    return (
        text,
        text,
        audio,
    )


def infer(
    project, file_checkpoint, exp_name, ref_text, ref_audio, gen_text, nfe_step, use_ema, speed, seed, remove_silence
):
    global last_checkpoint, last_device, tts_api, last_ema

    if not os.path.isfile(file_checkpoint):
        return None, "checkpoint not found!"

    if training_process is not None:
        device_test = "cpu"
    else:
        device_test = None

    if last_checkpoint != file_checkpoint or last_device != device_test or last_ema != use_ema or tts_api is None:
        if last_checkpoint != file_checkpoint:
            last_checkpoint = file_checkpoint

        if last_device != device_test:
            last_device = device_test

        if last_ema != use_ema:
            last_ema = use_ema

        vocab_file = os.path.join(path_data, project, "vocab.txt")

        tts_api = F5TTS(
            model=exp_name, ckpt_file=file_checkpoint, vocab_file=vocab_file, device=device_test, use_ema=use_ema
        )

        print("update >> ", device_test, file_checkpoint, use_ema)

    if seed == -1:  # -1 used for random
        seed = None

    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
        tts_api.infer(
            ref_file=ref_audio,
            ref_text=ref_text.strip(),
            gen_text=gen_text.strip(),
            nfe_step=nfe_step,
            speed=speed,
            remove_silence=remove_silence,
            file_wave=f.name,
            seed=seed,
        )
        return f.name, tts_api.device, str(tts_api.seed)


def check_finetune(finetune):
    return gr.update(interactive=finetune), gr.update(interactive=finetune), gr.update(interactive=finetune)


def get_checkpoints_project(project_name, is_gradio=True):
    if project_name is None:
        return [], ""
    project_name = project_name.replace("_pinyin", "").replace("_char", "")

    if os.path.isdir(path_project_ckpts):
        files_checkpoints = glob(os.path.join(path_project_ckpts, project_name, "*.pt"))
        # Separate pretrained and regular checkpoints
        pretrained_checkpoints = [f for f in files_checkpoints if "pretrained_" in os.path.basename(f)]
        regular_checkpoints = [
            f
            for f in files_checkpoints
            if "pretrained_" not in os.path.basename(f) and "model_last.pt" not in os.path.basename(f)
        ]
        last_checkpoint = [f for f in files_checkpoints if "model_last.pt" in os.path.basename(f)]

        # Sort regular checkpoints by number
        regular_checkpoints = sorted(
            regular_checkpoints, key=lambda x: int(os.path.basename(x).split("_")[1].split(".")[0])
        )

        # Combine in order: pretrained, regular, last
        files_checkpoints = pretrained_checkpoints + regular_checkpoints + last_checkpoint
    else:
        files_checkpoints = []

    selelect_checkpoint = None if not files_checkpoints else files_checkpoints[0]

    if is_gradio:
        return gr.update(choices=files_checkpoints, value=selelect_checkpoint)

    return files_checkpoints, selelect_checkpoint


def get_audio_project(project_name, is_gradio=True):
    if project_name is None:
        return [], ""
    project_name = project_name.replace("_pinyin", "").replace("_char", "")

    if os.path.isdir(path_project_ckpts):
        files_audios = glob(os.path.join(path_project_ckpts, project_name, "samples", "*.wav"))
        files_audios = sorted(files_audios, key=lambda x: int(os.path.basename(x).split("_")[1].split(".")[0]))

        files_audios = [item.replace("_gen.wav", "") for item in files_audios if item.endswith("_gen.wav")]
    else:
        files_audios = []

    selelect_checkpoint = None if not files_audios else files_audios[0]

    if is_gradio:
        return gr.update(choices=files_audios, value=selelect_checkpoint)

    return files_audios, selelect_checkpoint


def get_gpu_stats():
    gpu_stats = ""

    if torch.cuda.is_available():
        gpu_count = torch.cuda.device_count()
        for i in range(gpu_count):
            gpu_name = torch.cuda.get_device_name(i)
            gpu_properties = torch.cuda.get_device_properties(i)
            total_memory = gpu_properties.total_memory / (1024**3)  # in GB
            allocated_memory = torch.cuda.memory_allocated(i) / (1024**2)  # in MB
            reserved_memory = torch.cuda.memory_reserved(i) / (1024**2)  # in MB

            gpu_stats += (
                f"GPU {i} Name: {gpu_name}\n"
                f"Total GPU memory (GPU {i}): {total_memory:.2f} GB\n"
                f"Allocated GPU memory (GPU {i}): {allocated_memory:.2f} MB\n"
                f"Reserved GPU memory (GPU {i}): {reserved_memory:.2f} MB\n\n"
            )
    elif torch.xpu.is_available():
        gpu_count = torch.xpu.device_count()
        for i in range(gpu_count):
            gpu_name = torch.xpu.get_device_name(i)
            gpu_properties = torch.xpu.get_device_properties(i)
            total_memory = gpu_properties.total_memory / (1024**3)  # in GB
            allocated_memory = torch.xpu.memory_allocated(i) / (1024**2)  # in MB
            reserved_memory = torch.xpu.memory_reserved(i) / (1024**2)  # in MB

            gpu_stats += (
                f"GPU {i} Name: {gpu_name}\n"
                f"Total GPU memory (GPU {i}): {total_memory:.2f} GB\n"
                f"Allocated GPU memory (GPU {i}): {allocated_memory:.2f} MB\n"
                f"Reserved GPU memory (GPU {i}): {reserved_memory:.2f} MB\n\n"
            )
    elif torch.backends.mps.is_available():
        gpu_count = 1
        gpu_stats += "MPS GPU\n"
        total_memory = psutil.virtual_memory().total / (
            1024**3
        )  # Total system memory (MPS doesn't have its own memory)
        allocated_memory = 0
        reserved_memory = 0

        gpu_stats += (
            f"Total system memory: {total_memory:.2f} GB\n"
            f"Allocated GPU memory (MPS): {allocated_memory:.2f} MB\n"
            f"Reserved GPU memory (MPS): {reserved_memory:.2f} MB\n"
        )

    else:
        gpu_stats = "No GPU available"

    return gpu_stats


def get_cpu_stats():
    cpu_usage = psutil.cpu_percent(interval=1)
    memory_info = psutil.virtual_memory()
    memory_used = memory_info.used / (1024**2)
    memory_total = memory_info.total / (1024**2)
    memory_percent = memory_info.percent

    pid = os.getpid()
    process = psutil.Process(pid)
    nice_value = process.nice()

    cpu_stats = (
        f"CPU Usage: {cpu_usage:.2f}%\n"
        f"System Memory: {memory_used:.2f} MB used / {memory_total:.2f} MB total ({memory_percent}% used)\n"
        f"Process Priority (Nice value): {nice_value}"
    )

    return cpu_stats


def get_combined_stats():
    gpu_stats = get_gpu_stats()
    cpu_stats = get_cpu_stats()
    combined_stats = f"### GPU Stats\n{gpu_stats}\n\n### CPU Stats\n{cpu_stats}"
    return combined_stats


def get_audio_select(file_sample):
    select_audio_ref = file_sample
    select_audio_gen = file_sample

    if file_sample is not None:
        select_audio_ref += "_ref.wav"
        select_audio_gen += "_gen.wav"

    return select_audio_ref, select_audio_gen


with gr.Blocks() as app:
    gr.Markdown(
        """
# F5 TTS Automatic Finetune

This is a local web UI for F5 TTS finetuning support. This app supports the following TTS models:

* [F5-TTS](https://arxiv.org/abs/2410.06885) (A Fairytaler that Fakes Fluent and Faithful Speech with Flow Matching)
* [E2 TTS](https://arxiv.org/abs/2406.18009) (Embarrassingly Easy Fully Non-Autoregressive Zero-Shot TTS)

The pretrained checkpoints support English and Chinese.

For tutorial and updates check here (https://github.com/SWivid/F5-TTS/discussions/143)
"""
    )

    with gr.Row():
        projects, projects_selelect = get_list_projects()
        tokenizer_type = gr.Radio(label="Tokenizer Type", choices=["pinyin", "char", "custom"], value="pinyin")
        project_name = gr.Textbox(label="Project Name", value="my_speak")
        bt_create = gr.Button("Create a New Project")

    with gr.Row():
        cm_project = gr.Dropdown(
            choices=projects, value=projects_selelect, label="Project", allow_custom_value=True, scale=6
        )
        ch_refresh_project = gr.Button("Refresh", scale=1)

    bt_create.click(fn=create_data_project, inputs=[project_name, tokenizer_type], outputs=[cm_project])

    with gr.Tabs():
        with gr.TabItem("Transcribe Data"):
            gr.Markdown("""```plaintext 
Skip this step if you have your dataset, metadata.csv, and a folder wavs with all the audio files.                 
```""")

            ch_manual = gr.Checkbox(label="Audio from Path", value=False)

            mark_info_transcribe = gr.Markdown(
                """```plaintext    
     Place your 'wavs' folder and 'metadata.csv' file in the '{your_project_name}' directory. 
                 
     my_speak/
     │
     └── dataset/
         ├── audio1.wav
         └── audio2.wav
         ...
     ```""",
                visible=False,
            )

            audio_speaker = gr.File(label="Voice", type="filepath", file_count="multiple")
            txt_lang = gr.Textbox(label="Language", value="English")
            bt_transcribe = bt_create = gr.Button("Transcribe")
            txt_info_transcribe = gr.Textbox(label="Info", value="")
            bt_transcribe.click(
                fn=transcribe_all,
                inputs=[cm_project, audio_speaker, txt_lang, ch_manual],
                outputs=[txt_info_transcribe],
            )
            ch_manual.change(fn=check_user, inputs=[ch_manual], outputs=[audio_speaker, mark_info_transcribe])

            random_sample_transcribe = gr.Button("Random Sample")

            with gr.Row():
                random_text_transcribe = gr.Textbox(label="Text")
                random_audio_transcribe = gr.Audio(label="Audio", type="filepath")

            random_sample_transcribe.click(
                fn=get_random_sample_transcribe,
                inputs=[cm_project],
                outputs=[random_text_transcribe, random_audio_transcribe],
            )

        with gr.TabItem("Vocab Check"):
            gr.Markdown("""```plaintext 
Check the vocabulary for fine-tuning Emilia_ZH_EN to ensure all symbols are included. For fine-tuning a new language.
```""")

            check_button = gr.Button("Check Vocab")
            txt_info_check = gr.Textbox(label="Info", value="")

            gr.Markdown("""```plaintext 
Using the extended model, you can finetune to a new language that is missing symbols in the vocab. This creates a new model with a new vocabulary size and saves it in your ckpts/project folder.
```""")

            exp_name_extend = gr.Radio(
                label="Model", choices=["F5TTS_v1_Base", "F5TTS_Base", "E2TTS_Base"], value="F5TTS_v1_Base"
            )

            with gr.Row():
                txt_extend = gr.Textbox(
                    label="Symbols",
                    value="",
                    placeholder="To add new symbols, make sure to use ',' for each symbol",
                    scale=6,
                )
                txt_count_symbol = gr.Textbox(label="New Vocab Size", value="", scale=1)

            extend_button = gr.Button("Extend")
            txt_info_extend = gr.Textbox(label="Info", value="")

            txt_extend.change(vocab_count, inputs=[txt_extend], outputs=[txt_count_symbol])
            check_button.click(
                fn=vocab_check, inputs=[cm_project, tokenizer_type], outputs=[txt_info_check, txt_extend]
            )
            extend_button.click(
                fn=vocab_extend, inputs=[cm_project, txt_extend, exp_name_extend], outputs=[txt_info_extend]
            )

        with gr.TabItem("Prepare Data"):
            gr.Markdown("""```plaintext 
Skip this step if you have your dataset, raw.arrow, duration.json, and vocab.txt
```""")

            gr.Markdown(
                """```plaintext    
     Place all your "wavs" folder and your "metadata.csv" file in your project name directory.

     Supported audio formats: "wav", "mp3", "aac", "flac", "m4a", "alac", "ogg", "aiff", "wma", "amr"

     Example wav format:                               
     my_speak/
     │
     ├── wavs/
     │   ├── audio1.wav
     │   └── audio2.wav
     |   ...
     │
     └── metadata.csv
      
     File format metadata.csv:

     audio1|text1 or audio1.wav|text1 or your_path/audio1.wav|text1 
     audio2|text1 or audio2.wav|text1 or your_path/audio2.wav|text1 
     ...

     ```"""
            )
            ch_tokenizern = gr.Checkbox(label="Create Vocabulary", value=False, visible=False)

            bt_prepare = bt_create = gr.Button("Prepare")
            txt_info_prepare = gr.Textbox(label="Info", value="")
            txt_vocab_prepare = gr.Textbox(label="Vocab", value="")

            bt_prepare.click(
                fn=create_metadata, inputs=[cm_project, ch_tokenizern], outputs=[txt_info_prepare, txt_vocab_prepare]
            )

            random_sample_prepare = gr.Button("Random Sample")

            with gr.Row():
                random_text_prepare = gr.Textbox(label="Tokenizer")
                random_audio_prepare = gr.Audio(label="Audio", type="filepath")

            random_sample_prepare.click(
                fn=get_random_sample_prepare, inputs=[cm_project], outputs=[random_text_prepare, random_audio_prepare]
            )

        with gr.TabItem("Train Model"):
            gr.Markdown("""```plaintext 
The auto-setting is still experimental. Set a large value of epoch if not sure; and keep last N checkpoints if limited disk space.
If you encounter a memory error, try reducing the batch size per GPU to a smaller number.
```""")
            with gr.Row():
                exp_name = gr.Radio(label="Model", choices=["F5TTS_v1_Base", "F5TTS_Base", "E2TTS_Base"])
                tokenizer_file = gr.Textbox(label="Tokenizer File")
                file_checkpoint_train = gr.Textbox(label="Path to the Pretrained Checkpoint")

            with gr.Row():
                ch_finetune = bt_create = gr.Checkbox(label="Finetune")
                lb_samples = gr.Label(label="Samples")
                bt_calculate = bt_create = gr.Button("Auto Settings")

            with gr.Row():
                epochs = gr.Number(label="Epochs")
                learning_rate = gr.Number(label="Learning Rate", step=0.5e-5)
                max_grad_norm = gr.Number(label="Max Gradient Norm")
                num_warmup_updates = gr.Number(label="Warmup Updates")

            with gr.Row():
                batch_size_type = gr.Radio(
                    label="Batch Size Type",
                    choices=["frame", "sample"],
                    info="frame is calculated as seconds * sampling_rate / hop_length",
                )
                batch_size_per_gpu = gr.Number(label="Batch Size per GPU", info="N frames or N samples")
                grad_accumulation_steps = gr.Number(
                    label="Gradient Accumulation Steps", info="Effective batch size is multiplied by this value"
                )
                max_samples = gr.Number(label="Max Samples", info="Maximum number of samples per single GPU batch")

            with gr.Row():
                save_per_updates = gr.Number(
                    label="Save per Updates",
                    info="Save intermediate checkpoints every N updates",
                    minimum=10,
                )
                keep_last_n_checkpoints = gr.Number(
                    label="Keep Last N Checkpoints",
                    step=1,
                    precision=0,
                    info="-1 to keep all, 0 to not save intermediate, > 0 to keep last N",
                    minimum=-1,
                )
                last_per_updates = gr.Number(
                    label="Last per Updates",
                    info="Save latest checkpoint with suffix _last.pt every N updates",
                    minimum=10,
                )
                gr.Radio(label="")  # placeholder

            with gr.Row():
                ch_8bit_adam = gr.Checkbox(label="Use 8-bit Adam optimizer")
                mixed_precision = gr.Radio(label="Mixed Precision", choices=["none", "fp16", "bf16"])
                cd_logger = gr.Radio(label="Logger", choices=["none", "wandb", "tensorboard"])
                with gr.Column():
                    start_button = gr.Button("Start Training")
                    stop_button = gr.Button("Stop Training", interactive=False)

            if projects_selelect is not None:
                (
                    exp_name_value,
                    learning_rate_value,
                    batch_size_per_gpu_value,
                    batch_size_type_value,
                    max_samples_value,
                    grad_accumulation_steps_value,
                    max_grad_norm_value,
                    epochs_value,
                    num_warmup_updates_value,
                    save_per_updates_value,
                    keep_last_n_checkpoints_value,
                    last_per_updates_value,
                    finetune_value,
                    file_checkpoint_train_value,
                    tokenizer_type_value,
                    tokenizer_file_value,
                    mixed_precision_value,
                    logger_value,
                    bnb_optimizer_value,
                ) = load_settings(projects_selelect)

                # Assigning values to the respective components
                exp_name.value = exp_name_value
                learning_rate.value = learning_rate_value
                batch_size_per_gpu.value = batch_size_per_gpu_value
                batch_size_type.value = batch_size_type_value
                max_samples.value = max_samples_value
                grad_accumulation_steps.value = grad_accumulation_steps_value
                max_grad_norm.value = max_grad_norm_value
                epochs.value = epochs_value
                num_warmup_updates.value = num_warmup_updates_value
                save_per_updates.value = save_per_updates_value
                keep_last_n_checkpoints.value = keep_last_n_checkpoints_value
                last_per_updates.value = last_per_updates_value
                ch_finetune.value = finetune_value
                file_checkpoint_train.value = file_checkpoint_train_value
                tokenizer_type.value = tokenizer_type_value
                tokenizer_file.value = tokenizer_file_value
                mixed_precision.value = mixed_precision_value
                cd_logger.value = logger_value
                ch_8bit_adam.value = bnb_optimizer_value

            ch_stream = gr.Checkbox(label="Stream Output Experiment", value=True)
            txt_info_train = gr.Textbox(label="Info", value="")

            list_audios, select_audio = get_audio_project(projects_selelect, False)

            select_audio_ref = select_audio
            select_audio_gen = select_audio

            if select_audio is not None:
                select_audio_ref += "_ref.wav"
                select_audio_gen += "_gen.wav"

            with gr.Row():
                ch_list_audio = gr.Dropdown(
                    choices=list_audios,
                    value=select_audio,
                    label="Audios",
                    allow_custom_value=True,
                    scale=6,
                    interactive=True,
                )
                bt_stream_audio = gr.Button("Refresh", scale=1)
                bt_stream_audio.click(fn=get_audio_project, inputs=[cm_project], outputs=[ch_list_audio])
                cm_project.change(fn=get_audio_project, inputs=[cm_project], outputs=[ch_list_audio])

            with gr.Row():
                audio_ref_stream = gr.Audio(label="Original", type="filepath", value=select_audio_ref)
                audio_gen_stream = gr.Audio(label="Generate", type="filepath", value=select_audio_gen)

            ch_list_audio.change(
                fn=get_audio_select,
                inputs=[ch_list_audio],
                outputs=[audio_ref_stream, audio_gen_stream],
            )

            start_button.click(
                fn=start_training,
                inputs=[
                    cm_project,
                    exp_name,
                    learning_rate,
                    batch_size_per_gpu,
                    batch_size_type,
                    max_samples,
                    grad_accumulation_steps,
                    max_grad_norm,
                    epochs,
                    num_warmup_updates,
                    save_per_updates,
                    keep_last_n_checkpoints,
                    last_per_updates,
                    ch_finetune,
                    file_checkpoint_train,
                    tokenizer_type,
                    tokenizer_file,
                    mixed_precision,
                    ch_stream,
                    cd_logger,
                    ch_8bit_adam,
                ],
                outputs=[txt_info_train, start_button, stop_button],
            )
            stop_button.click(fn=stop_training, outputs=[txt_info_train, start_button, stop_button])

            bt_calculate.click(
                fn=calculate_train,
                inputs=[
                    cm_project,
                    epochs,
                    learning_rate,
                    batch_size_per_gpu,
                    batch_size_type,
                    max_samples,
                    num_warmup_updates,
                    ch_finetune,
                ],
                outputs=[
                    epochs,
                    learning_rate,
                    batch_size_per_gpu,
                    max_samples,
                    num_warmup_updates,
                    lb_samples,
                ],
            )

            ch_finetune.change(
                check_finetune, inputs=[ch_finetune], outputs=[file_checkpoint_train, tokenizer_file, tokenizer_type]
            )

            def setup_load_settings():
                output_components = [
                    exp_name,
                    learning_rate,
                    batch_size_per_gpu,
                    batch_size_type,
                    max_samples,
                    grad_accumulation_steps,
                    max_grad_norm,
                    epochs,
                    num_warmup_updates,
                    save_per_updates,
                    keep_last_n_checkpoints,
                    last_per_updates,
                    ch_finetune,
                    file_checkpoint_train,
                    tokenizer_type,
                    tokenizer_file,
                    mixed_precision,
                    cd_logger,
                    ch_8bit_adam,
                ]
                return output_components

            outputs = setup_load_settings()

            cm_project.change(
                fn=load_settings,
                inputs=[cm_project],
                outputs=outputs,
            )

            ch_refresh_project.click(
                fn=load_settings,
                inputs=[cm_project],
                outputs=outputs,
            )

        with gr.TabItem("Test Model"):
            gr.Markdown("""```plaintext 
Check the use_ema setting (True or False) for your model to see what works best for you. Set seed to -1 for random.
```""")
            exp_name = gr.Radio(
                label="Model", choices=["F5TTS_v1_Base", "F5TTS_Base", "E2TTS_Base"], value="F5TTS_v1_Base"
            )
            list_checkpoints, checkpoint_select = get_checkpoints_project(projects_selelect, False)

            with gr.Row():
                nfe_step = gr.Number(label="NFE Step", value=32)
                speed = gr.Slider(label="Speed", value=1.0, minimum=0.3, maximum=2.0, step=0.1)
                seed = gr.Number(label="Random Seed", value=-1, minimum=-1)
                remove_silence = gr.Checkbox(label="Remove Silence")

            with gr.Row():
                ch_use_ema = gr.Checkbox(
                    label="Use EMA", value=True, info="Turn off at early stage might offer better results"
                )
                cm_checkpoint = gr.Dropdown(
                    choices=list_checkpoints, value=checkpoint_select, label="Checkpoints", allow_custom_value=True
                )
                bt_checkpoint_refresh = gr.Button("Refresh")

            random_sample_infer = gr.Button("Random Sample")

            ref_text = gr.Textbox(label="Reference Text")
            ref_audio = gr.Audio(label="Reference Audio", type="filepath")
            gen_text = gr.Textbox(label="Text to Generate")

            random_sample_infer.click(
                fn=get_random_sample_infer, inputs=[cm_project], outputs=[ref_text, gen_text, ref_audio]
            )

            with gr.Row():
                txt_info_gpu = gr.Textbox("", label="Inference on Device :")
                seed_info = gr.Textbox(label="Used Random Seed :")
                check_button_infer = gr.Button("Inference")

            gen_audio = gr.Audio(label="Generated Audio", type="filepath")

            check_button_infer.click(
                fn=infer,
                inputs=[
                    cm_project,
                    cm_checkpoint,
                    exp_name,
                    ref_text,
                    ref_audio,
                    gen_text,
                    nfe_step,
                    ch_use_ema,
                    speed,
                    seed,
                    remove_silence,
                ],
                outputs=[gen_audio, txt_info_gpu, seed_info],
            )

            bt_checkpoint_refresh.click(fn=get_checkpoints_project, inputs=[cm_project], outputs=[cm_checkpoint])
            cm_project.change(fn=get_checkpoints_project, inputs=[cm_project], outputs=[cm_checkpoint])

        with gr.TabItem("Prune Checkpoint"):
            gr.Markdown("""```plaintext 
Reduce the Base model size from 5GB to 1.3GB. The new checkpoint file prunes out optimizer and etc., can be used for inference or finetuning afterward, but not able to resume pretraining.
```""")
            txt_path_checkpoint = gr.Textbox(label="Path to Checkpoint:")
            txt_path_checkpoint_small = gr.Textbox(label="Path to Output:")
            with gr.Row():
                ch_save_ema = gr.Checkbox(label="Save EMA checkpoint", value=True)
                ch_safetensors = gr.Checkbox(label="Save with safetensors format", value=True)
            txt_info_reduse = gr.Textbox(label="Info", value="")
            reduse_button = gr.Button("Prune")
            reduse_button.click(
                fn=prune_checkpoint,
                inputs=[txt_path_checkpoint, txt_path_checkpoint_small, ch_save_ema, ch_safetensors],
                outputs=[txt_info_reduse],
            )

        with gr.TabItem("System Info"):
            output_box = gr.Textbox(label="GPU and CPU Information", lines=20)

            def update_stats():
                return get_combined_stats()

            update_button = gr.Button("Update Stats")
            update_button.click(fn=update_stats, outputs=output_box)

            def auto_update():
                yield gr.update(value=update_stats())

            gr.update(fn=auto_update, inputs=[], outputs=output_box)


@click.command()
@click.option("--port", "-p", default=None, type=int, help="Port to run the app on")
@click.option("--host", "-H", default=None, help="Host to run the app on")
@click.option(
    "--share",
    "-s",
    default=False,
    is_flag=True,
    help="Share the app via Gradio share link",
)
@click.option("--api", "-a", default=True, is_flag=True, help="Allow API access")
def main(port, host, share, api):
    global app
    print("Starting app...")
    app.queue(api_open=api).launch(server_name=host, server_port=port, share=share, show_api=api)


if __name__ == "__main__":
    main()
