"""Simple webcam attendance and ADB call prototype."""

import os
import re
import subprocess
import threading
import time
import tkinter as tk
import winsound
import wave
from pathlib import Path
from tkinter import messagebox, simpledialog

import cv2
from PIL import Image, ImageTk, UnidentifiedImageError


ADB_PATH = Path(__file__).parent / "platform-tools" / "adb.exe"
FACE_MODEL_PATH = Path(__file__).parent / "models" / "haarcascade_frontalface_default.xml"
PAPPU_CUTOUT_PATH = Path(__file__).parent / "pappuvideo.mp4"
VOICE_PATH = Path(__file__).parent / "pappu.wav"
FLOWER_STICKER_PATH = Path(__file__).parent / "flower_sticker.png"
SNAPSHOT_DIR = Path(__file__).parent / "snapshots"

CALL_WAIT_SECONDS = 60
CALL_AUDIO_FALLBACK_SECONDS = 10
ADB_RECONNECT_SECONDS = 8


def normalize_phone_number(value: str) -> str:
	"""Return a phone number suitable for a tel: URI or raise ValueError."""
	number = value.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
	if not re.fullmatch(r"\+?[0-9]{7,15}", number):
		raise ValueError("Enter a valid phone number with 7 to 15 digits.")
	return number


def run_adb(*arguments: str) -> subprocess.CompletedProcess[str]:
	if not ADB_PATH.exists():
		raise RuntimeError(f"ADB was not found at: {ADB_PATH}")
	return subprocess.run(
		[str(ADB_PATH), *arguments],
		capture_output=True,
		text=True,
		check=True,
	)


def run_adb_optional(*arguments: str) -> str:
	"""Return ADB output when Android permits the query, otherwise return empty."""
	result = subprocess.run(
		[str(ADB_PATH), *arguments],
		capture_output=True,
		text=True,
	)
	return result.stdout if result.returncode == 0 else ""


def ensure_adb_device(timeout: int = ADB_RECONNECT_SECONDS) -> None:
	"""Refresh ADB and wait briefly for an authorized phone to reappear."""
	deadline = time.monotonic() + timeout
	last_status = "not detected"
	while time.monotonic() < deadline:
		result = subprocess.run(
			[str(ADB_PATH), "devices"],
			capture_output=True,
			text=True,
		)
		if re.search(r"\n[^\s]+\s+device(?:\s|$)", result.stdout):
			return
		if re.search(r"\n[^\s]+\s+unauthorized(?:\s|$)", result.stdout):
			last_status = "unauthorized"
		elif re.search(r"\n[^\s]+\s+offline(?:\s|$)", result.stdout):
			last_status = "offline"
		subprocess.run([str(ADB_PATH), "reconnect", "device"], capture_output=True, text=True)
		time.sleep(1)
	if last_status == "offline":
		raise RuntimeError("Android phone is offline. Unlock it, reconnect the USB cable, and accept USB debugging.")
	if last_status == "unauthorized":
		raise RuntimeError("Android phone is not authorized. Unlock it and accept the USB debugging prompt.")
	raise RuntimeError("Android phone was not detected. Check the USB cable and USB debugging.")


def start_call(phone_number: str) -> None:
	ensure_adb_device()
	run_adb(
		"shell",
		"am",
		"start",
		"-a",
		"android.intent.action.CALL",
		"-d",
		f"tel:{phone_number}",
	)


def wait_for_call_active(timeout: int = CALL_WAIT_SECONDS) -> None:
	"""Wait until Android reports an answered/off-hook call."""
	deadline = time.monotonic() + timeout
	can_read_state = False
	while time.monotonic() < deadline:
		telephony = run_adb_optional("shell", "dumpsys", "telephony.registry")
		if telephony:
			can_read_state = True
		else:
			break
		call_is_active = (
			re.search(r"mCallState=2|OFFHOOK", telephony, re.IGNORECASE)
		)
		if call_is_active:
			return
		time.sleep(1)
	if not can_read_state:
		time.sleep(CALL_AUDIO_FALLBACK_SECONDS)
		return
	raise TimeoutError("The call was not answered within 60 seconds.")


def play_voice_message() -> None:
	"""Play the local WAV file through the Windows default audio device."""
	if not VOICE_PATH.exists():
		raise RuntimeError(f"Audio file was not found at: {VOICE_PATH}")
	winsound.PlaySound(str(VOICE_PATH), winsound.SND_FILENAME)


def voice_message_duration() -> float:
	with wave.open(str(VOICE_PATH), "rb") as audio:
		return audio.getnframes() / audio.getframerate()


def show_visual_screen(
	title: str,
	message: str,
	image_path: Path,
	background: str,
	foreground: str,
	audio_path: Path | None = None,
) -> tk.Toplevel:
	visual_window = tk.Toplevel()
	visual_window.title(title)
	visual_window.geometry("640x480")
	visual_window.configure(bg=background)
	visual_window.protocol("WM_DELETE_WINDOW", visual_window.destroy)
	label = tk.Label(visual_window, bg=background)
	label.pack(expand=True, pady=(20, 4))
	animation = None
	try:
		if image_path.suffix.lower() == ".mp4":
			# MP4 audio is intentionally muted; the WAV file is played separately.
			animation = VideoAnimation(visual_window, label, image_path, None)
		else:
			animation = GifAnimation(visual_window, label, image_path)
		visual_window.animation = animation
		animation.start()
	except (OSError, UnidentifiedImageError, tk.TclError):
		label.configure(
			text=message,
			font=("Segoe UI", 28, "bold"),
			fg=foreground,
			justify="center",
		)
	return visual_window


class VideoAnimation:
	def __init__(self, window: tk.Toplevel, label: tk.Label, path: Path, audio_path: Path | None = None) -> None:
		self.window = window
		self.label = label
		self.path = path
		self.audio_path = audio_path
		self.capture = cv2.VideoCapture(str(path))
		if not self.capture.isOpened():
			self.capture.release()
			raise OSError(f"Could not open video: {path}")
		self.fps = max(self.capture.get(cv2.CAP_PROP_FPS), 1.0)
		self.after_id: str | None = None
		self.audio_thread: threading.Thread | None = None

	def start(self) -> None:
		if self.audio_path is not None:
			if not self.audio_path.exists():
				raise OSError(f"Audio file was not found at: {self.audio_path}")
			self.audio_thread = threading.Thread(target=self.play_audio, daemon=True)
			self.audio_thread.start()
		self.show_frame()

	def play_audio(self) -> None:
		winsound.PlaySound(str(self.audio_path), winsound.SND_FILENAME)

	def show_frame(self) -> None:
		if not self.window.winfo_exists():
			self.capture.release()
			winsound.PlaySound(None, winsound.SND_PURGE)
			return
		success, frame = self.capture.read()
		if not success:
			self.capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
			success, frame = self.capture.read()
		if success:
			frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
			image = Image.fromarray(frame)
			image.thumbnail((600, 380), Image.Resampling.LANCZOS)
			photo = ImageTk.PhotoImage(image)
			self.label.configure(image=photo)
			self.label.image = photo
		self.after_id = self.window.after(max(15, int(1000 / self.fps)), self.show_frame)



class GifAnimation:
	def __init__(self, window: tk.Toplevel, label: tk.Label, path: Path) -> None:
		self.window = window
		self.label = label
		self.frames: list[ImageTk.PhotoImage] = []
		self.durations: list[int] = []
		self.index = 0
		self.after_id: str | None = None
		with Image.open(path) as source:
			for frame_index in range(getattr(source, "n_frames", 1)):
				source.seek(frame_index)
				frame = source.convert("RGBA")
				frame.thumbnail((600, 380), Image.Resampling.LANCZOS)
				self.frames.append(ImageTk.PhotoImage(frame))
				self.durations.append(max(50, source.info.get("duration", 100)))
		if not self.frames:
			raise UnidentifiedImageError(f"GIF has no frames: {path}")

	def start(self) -> None:
		self.show_frame()

	def show_frame(self) -> None:
		if not self.window.winfo_exists():
			return
		self.label.configure(image=self.frames[self.index])
		self.after_id = self.window.after(self.durations[self.index], self.show_frame)
		self.index = (self.index + 1) % len(self.frames)


def apply_sticker(frame, x: int, y: int, width: int, height: int):
	if not FLOWER_STICKER_PATH.exists():
		return frame
	sticker = cv2.imread(str(FLOWER_STICKER_PATH), cv2.IMREAD_UNCHANGED)
	if sticker is None or sticker.shape[2] != 4:
		return frame
	sticker_width = max(int(width * 0.55), 1)
	sticker_height = max(int(sticker.shape[0] * sticker_width / sticker.shape[1]), 1)
	sticker = cv2.resize(sticker, (sticker_width, sticker_height), interpolation=cv2.INTER_AREA)
	# Place the flower sticker very close to the right ear of the detected face.
	anchor_x = x + width - int(sticker_width * 0.95)
	anchor_y = y + int(height * 0.12) - int(sticker_height * 0.05)
	start_x = max(0, anchor_x)
	start_y = max(0, anchor_y)
	end_x = min(frame.shape[1], anchor_x + sticker_width)
	end_y = min(frame.shape[0], start_y + sticker_height)
	if start_x >= end_x or start_y >= end_y:
		return frame
	sticker_x = start_x - anchor_x
	sticker_y = start_y - anchor_y
	region = sticker[sticker_y:sticker_y + end_y - start_y, sticker_x:sticker_x + end_x - start_x]
	alpha = region[:, :, 3:4] / 255.0
	frame[start_y:end_y, start_x:end_x] = (
		region[:, :, :3] * alpha + frame[start_y:end_y, start_x:end_x] * (1 - alpha)
	).astype("uint8")
	return frame


def show_prompt_screen() -> tk.Toplevel:
	return show_visual_screen(
		"Question",
		"",
		PAPPU_CUTOUT_PATH,
		"#fff7ed",
		"#9a3412",
	)


def show_snapshot_screen(snapshot_path: Path) -> tk.Toplevel:
	window = tk.Toplevel()
	window.title("Captured attendance photo")
	window.geometry("640x520")
	window.configure(bg="#111827")
	window.protocol("WM_DELETE_WINDOW", window.destroy)
	label = tk.Label(window, text="Captured photo", bg="#111827", fg="white", font=("Segoe UI", 16, "bold"))
	label.pack(pady=(14, 6))
	image_label = tk.Label(window, bg="#111827")
	image_label.pack(expand=True, padx=20, pady=(0, 20))
	try:
		with Image.open(snapshot_path) as source:
			image = source.convert("RGB")
			image.thumbnail((600, 420), Image.Resampling.LANCZOS)
			photo = ImageTk.PhotoImage(image)
		image_label.configure(image=photo)
		image_label.image = photo
	except (OSError, UnidentifiedImageError, tk.TclError):
		image_label.configure(text=f"Could not open captured photo:\n{snapshot_path}", fg="#fca5a5", font=("Segoe UI", 12))
	return window


class AttendanceApp:
	def __init__(self, root: tk.Tk) -> None:
		self.root = root
		self.root.title("Attendance Call")
		self.root.protocol("WM_DELETE_WINDOW", self.close)
		self.camera = cv2.VideoCapture(0, cv2.CAP_DSHOW)
		if not self.camera.isOpened():
			raise RuntimeError("The webcam could not be opened. Close other camera apps and retry.")
		if not FACE_MODEL_PATH.exists():
			raise RuntimeError(f"Face model was not found at: {FACE_MODEL_PATH}")
		self.detector = cv2.CascadeClassifier(str(FACE_MODEL_PATH))
		if self.detector.empty():
			raise RuntimeError("OpenCV could not load the face model.")
		self.face_found = False
		self.phone_number: str | None = None
		self.busy = False
		self.running = True
		self.prompt_window: tk.Toplevel | None = None
		self.prompt_after_id: str | None = None
		self.snapshot_path: Path | None = None
		self.snapshot_taken = False
		self.status = tk.StringVar(value="Looking for a face...")
		tk.Label(root, textvariable=self.status, font=("Segoe UI", 16)).pack(pady=8)
		tk.Label(
			root,
			text="When a face is detected, enter the phone number, then press Space to call.",
			font=("Segoe UI", 11),
		).pack(pady=(0, 8))
		root.bind("<space>", self.call_from_key)
		root.bind("<Escape>", lambda _event: self.close())
		self.update_frame()

	def update_frame(self) -> None:
		if not self.running:
			return
		success, frame = self.camera.read()
		if not success:
			self.status.set("Cannot read the webcam.")
			self.root.after(100, self.update_frame)
			return
		gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
		faces = self.detector.detectMultiScale(gray, 1.1, 5, minSize=(60, 60))
		self.face_found = len(faces) > 0
		snapshot_frame = frame.copy()
		for x, y, width, height in faces:
			cv2.rectangle(frame, (x, y), (x + width, y + height), (0, 200, 0), 3)
		if self.face_found and not self.snapshot_taken:
			SNAPSHOT_DIR.mkdir(exist_ok=True)
			self.snapshot_path = SNAPSHOT_DIR / time.strftime("face_%Y%m%d_%H%M%S.jpg")
			for x, y, width, height in faces:
				snapshot_frame = apply_sticker(snapshot_frame, x, y, width, height)
			self.snapshot_taken = cv2.imwrite(str(self.snapshot_path), snapshot_frame)
			if not self.snapshot_taken:
				self.snapshot_path = None
		if not self.busy:
			if self.face_found and self.phone_number is None:
				self.status.set("Face detected. Phone number required: press Space.")
			elif self.phone_number:
				self.status.set("Attendance registering, click Space bar to call.")
			else:
				self.status.set("Looking for a face...")
		cv2.imshow("Webcam - press Escape to exit", frame)
		key = cv2.waitKey(1) & 0xFF
		if key == 32:
			self.call_from_key(None)
		if key == 27:
			self.close()
			return
		self.root.after(15, self.update_frame)

	def call_from_key(self, _event: tk.Event) -> None:
		if self.busy or not self.face_found:
			return
		if self.phone_number is None:
			value = simpledialog.askstring("Phone number required", "Enter phone number:")
			if value is None:
				return
			try:
				self.phone_number = normalize_phone_number(value)
			except ValueError as error:
				messagebox.showerror("Invalid phone number", str(error))
				return
			self.start_call()
			return
		self.start_call()

	def start_call(self) -> None:
		if self.busy or self.phone_number is None:
			return
		self.busy = True
		self.status.set("Calling... keep the phone on speakerphone near the PC speaker.")
		threading.Thread(target=self.call_workflow, daemon=True).start()

	def call_workflow(self) -> None:
		try:
			start_call(self.phone_number or "")
			self.root.after(0, lambda: self.status.set("Waiting for the attendee to answer..."))
			wait_for_call_active()
			self.root.after(0, self.open_prompt_screen)
			self.root.after(0, lambda: self.status.set("Call answered. Playing the voice message..."))
		except Exception as error:
			self.root.after(0, lambda: messagebox.showerror("Call failed", str(error)))
			self.root.after(0, lambda: self.status.set("Call failed. Check the phone and ADB connection."))
			self.root.after(0, self.reset_for_next_person)
		finally:
			try:
				ensure_adb_device()
			except RuntimeError:
				pass

	def reset_for_next_person(self) -> None:
		self.busy = False
		self.phone_number = None
		self.snapshot_path = None
		self.snapshot_taken = False

	def open_prompt_screen(self) -> None:
		self.prompt_window = show_prompt_screen()
		self.root.after(250, self.play_prompt_audio)
		try:
			duration_ms = max(100, int(voice_message_duration() * 1000))
		except (OSError, wave.Error, ZeroDivisionError):
			duration_ms = 1000
		self.prompt_after_id = self.root.after(duration_ms, self.finish_prompt_screen)

	def play_prompt_audio(self) -> None:
		try:
			play_voice_message()
		except RuntimeError:
			pass

	def finish_prompt_screen(self) -> None:
		self.prompt_after_id = None
		self.close_prompt_screen()
		snapshot_path = self.snapshot_path
		if snapshot_path is not None:
			show_snapshot_screen(snapshot_path)
		self.status.set("Call message played. Ready for the next person.")
		self.reset_for_next_person()

	def close_prompt_screen(self) -> None:
		if self.prompt_after_id is not None:
			self.root.after_cancel(self.prompt_after_id)
			self.prompt_after_id = None
		if self.prompt_window is not None and self.prompt_window.winfo_exists():
			self.prompt_window.destroy()
		self.prompt_window = None

	def close(self) -> None:
		self.running = False
		self.close_prompt_screen()
		self.camera.release()
		cv2.destroyAllWindows()
		self.root.destroy()


if __name__ == "__main__":
	os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")
	app_root = tk.Tk()
	AttendanceApp(app_root)
	app_root.mainloop()
