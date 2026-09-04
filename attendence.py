"""Simple webcam attendance and ADB call prototype."""

import os
import re
import math
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
SNAPSHOT_AUDIO_PATH = Path(__file__).parent / "pha.wav"
FLOWER_STICKER_PATH = Path(__file__).parent / "flower_sticker.png"
SNAPSHOT_DIR = Path(__file__).parent / "snapshots"

CALL_WAIT_SECONDS = 60
CALL_AUDIO_FALLBACK_SECONDS = 10
ADB_RECONNECT_SECONDS = 8
VIDEO_DELAY_AFTER_ANSWER_SECONDS = 9


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
		encoding="utf-8",
		errors="replace",
		check=True,
	)


def run_adb_optional(*arguments: str) -> str:
	"""Return ADB output when Android permits the query, otherwise return empty."""
	result = subprocess.run(
		[str(ADB_PATH), *arguments],
		capture_output=True,
		text=True,
		encoding="utf-8",
		errors="replace",
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
			encoding="utf-8",
			errors="replace",
		)
		if re.search(r"\n[^\s]+\s+device(?:\s|$)", result.stdout):
			return
		if re.search(r"\n[^\s]+\s+unauthorized(?:\s|$)", result.stdout):
			last_status = "unauthorized"
		elif re.search(r"\n[^\s]+\s+offline(?:\s|$)", result.stdout):
			last_status = "offline"
		subprocess.run(
			[str(ADB_PATH), "reconnect", "device"],
			capture_output=True,
			text=True,
			encoding="utf-8",
			errors="replace",
		)
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
	"""Wait until Android reports that the call has been answered."""
	deadline = time.monotonic() + timeout
	state_was_read = False
	while time.monotonic() < deadline:
		telecom = run_adb_optional("shell", "dumpsys", "telecom")
		if telecom:
			state_was_read = True
		else:
			time.sleep(1)
			continue
		if re.search(r"\b(?:state|State)\s*[:=]\s*ACTIVE\b", telecom):
			return
		time.sleep(1)
	if not state_was_read:
		raise RuntimeError("Could not verify whether the call was answered. Check ADB and try again.")
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
	message_label = tk.Label(
		visual_window,
		text=message,
		bg=background,
		fg=foreground,
		font=("Segoe UI", 24, "bold"),
	)
	message_label.pack(pady=(16, 4))
	label = tk.Label(visual_window, bg=background)
	label.pack(expand=True, pady=(0, 12))
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
			text="Video could not be loaded",
			font=("Segoe UI", 18, "bold"),
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
	sticker_width = max(int(width * 0.825), 1)
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
	window.title("Absent today")
	window.geometry("640x520")
	window.configure(bg="#111827")
	window.protocol("WM_DELETE_WINDOW", window.destroy)
	label = tk.Label(window, text="You are absent today", bg="#111827", fg="#fca5a5", font=("Segoe UI", 16, "bold"))
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
	if SNAPSHOT_AUDIO_PATH.exists():
		winsound.PlaySound(str(SNAPSHOT_AUDIO_PATH), winsound.SND_FILENAME | winsound.SND_ASYNC)
	return window


def show_fan_animation(on_complete) -> tk.Toplevel:
	window = tk.Toplevel()
	window.title("Cooling down")
	window.geometry("640x520")
	window.configure(bg="#dbeafe")
	window.protocol("WM_DELETE_WINDOW", window.destroy)
	tk.Label(window, text="Cooling down before the snapshot...", bg="#dbeafe", fg="#1e3a8a", font=("Segoe UI", 16, "bold")).pack(pady=(18, 8))
	tk.Label(window, text="you  re so hot ,u need some kaatt", bg="#dbeafe", fg="#1e3a8a", font=("Segoe UI", 14)).pack(pady=(0, 8))
	canvas = tk.Canvas(window, width=420, height=360, bg="#dbeafe", highlightthickness=0)
	canvas.pack(expand=True)
	center_x, center_y = 210, 180
	blade_length = 125
	blade_width = 34
	angle = 0
	completed = False

	def finish() -> None:
		nonlocal completed
		if completed:
			return
		completed = True
		if window.winfo_exists():
			window.destroy()
		on_complete()

	def animate() -> None:
		nonlocal angle
		if not window.winfo_exists() or completed:
			return
		canvas.delete("all")
		canvas.create_oval(center_x - 18, center_y - 18, center_x + 18, center_y + 18, fill="#334155", outline="")
		for blade_index in range(4):
			blade_angle = math.radians(angle + blade_index * 90 - 90)
			perpendicular_x = -math.sin(blade_angle)
			perpendicular_y = math.cos(blade_angle)
			direction_x = math.cos(blade_angle)
			direction_y = math.sin(blade_angle)
			canvas.create_polygon(
				center_x + perpendicular_x * blade_width / 2,
				center_y + perpendicular_y * blade_width / 2,
				center_x + direction_x * blade_length + perpendicular_x * blade_width / 2,
				center_y + direction_y * blade_length + perpendicular_y * blade_width / 2,
				center_x + direction_x * blade_length - perpendicular_x * blade_width / 2,
				center_y + direction_y * blade_length - perpendicular_y * blade_width / 2,
				center_x - perpendicular_x * blade_width / 2,
				center_y - perpendicular_y * blade_width / 2,
				fill="#2563eb",
				outline="#1d4ed8",
				width=2,
			)
		canvas.create_oval(center_x - 10, center_y - 10, center_x + 10, center_y + 10, fill="#f59e0b", outline="")
		angle = (angle + 12) % 360
		window.after(40, animate)

	animate()
	window.after(10000, finish)
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
			self.root.after(0, lambda: self.status.set("Call answered. Starting video in 9 seconds..."))
			time.sleep(VIDEO_DELAY_AFTER_ANSWER_SECONDS)
			self.root.after(0, self.open_prompt_screen)
			self.root.after(0, lambda: self.status.set("Call answered. Playing the video and voice message..."))
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
			if not VOICE_PATH.exists():
				raise RuntimeError(f"Audio file was not found at: {VOICE_PATH}")
		except RuntimeError:
			pass
		else:
			threading.Thread(target=play_voice_message, daemon=True).start()

	def finish_prompt_screen(self) -> None:
		self.prompt_after_id = None
		self.close_prompt_screen()
		snapshot_path = self.snapshot_path
		if snapshot_path is not None:
			show_fan_animation(lambda: show_snapshot_screen(snapshot_path))
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
