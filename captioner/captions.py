"""Grounded caption generation from local evidence (frames, speech context, file metadata)."""

from __future__ import annotations

import base64
import importlib.util
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import models, photo, tools, video
from .config import CaptionConfig

log = logging.getLogger("captioner.captions")
FRAMES_PER_VISION_CALL = 4


class CaptionUnavailableError(RuntimeError):
    """Local models are not reachable right now; the job should retry later."""


@dataclass(frozen=True)
class CaptionResult:
    caption: str | None
    source: str | None  # 'generated', 'metadata' or None (owner must write one)
    model_meta: dict = field(default_factory=dict)
    note: str = ""


@dataclass
class Evidence:
    descriptions: list[str]
    transcript: str
    date_text: str
    location_text: str
    notes: list[str]

    def block(self) -> str:
        return models.evidence_block(self.descriptions, self.transcript, self.date_text, self.location_text)


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


class LocalCaptioner:
    def __init__(self, cfg: CaptionConfig, ollama: models.OllamaClient | None = None,
                 transcriber: Callable[[Path, str, float], models.Transcript] = models.transcribe,
                 whisper_available: bool | None = None):
        self.cfg = cfg
        self.ollama = ollama or models.OllamaClient(cfg.ollama_url, timeout=cfg.model_timeout_s)
        self.transcriber = transcriber
        self.whisper_available = (importlib.util.find_spec("faster_whisper") is not None
                                  if whisper_available is None else whisper_available)

    # -- evidence -------------------------------------------------------------------------------
    def _describe(self, images: list[Path], what: str) -> str:
        prompt = models.VISION_PROMPT.format(what=what)
        return self.ollama.chat(self.cfg.vision_model, prompt, images_b64=[_b64(p) for p in images]).strip()

    def _location(self, gps: tuple[float, float] | None) -> str:
        if not self.cfg.include_location or gps is None:
            return ""
        return f"{gps[0]:.4f}, {gps[1]:.4f} (coordinates only)"

    def picture_evidence(self, path: Path, work: Path) -> Evidence:
        meta = photo.read_exif(path)
        ev = Evidence([], "", photo.date_text(meta.taken_at), self._location(meta.gps), [])
        thumb = photo.thumbnail(path, work / "thumb.jpg")
        if thumb is None:
            ev.notes.append("picture format not readable for analysis")
            return ev
        try:
            ev.descriptions.append(self._describe([thumb], "this photo"))
        except models.ModelUnreachableError as exc:
            raise CaptionUnavailableError(str(exc)) from exc
        except models.ModelError as exc:
            ev.notes.append(f"vision model failed: {type(exc).__name__}")
        return ev

    def video_evidence(self, path: Path, work: Path) -> Evidence:
        try:
            info = video.probe(path)
        except video.VideoError:
            return Evidence([], "", "", "", ["video could not be read"])
        ev = Evidence([], "", photo.date_text(info.created), "", [])
        try:
            times = video.frame_times(info.duration, self.cfg.frame_interval_s, self.cfg.max_frames)
            frames = video.extract_frames(path, times, work / "frames")
        except (video.VideoError, tools.ToolError) as exc:
            frames = []
            ev.notes.append(f"frame extraction failed: {type(exc).__name__}")
        for start in range(0, len(frames), FRAMES_PER_VISION_CALL):
            group = frames[start:start + FRAMES_PER_VISION_CALL]
            try:
                ev.descriptions.append(self._describe(group, "these consecutive frames from one video"))
            except models.ModelUnreachableError as exc:
                raise CaptionUnavailableError(str(exc)) from exc
            except models.ModelError as exc:
                ev.notes.append(f"vision model failed: {type(exc).__name__}")
                break
        if info.has_audio and self.whisper_available:
            try:
                audio = video.extract_audio(path, work / "audio.wav")
                ev.transcript = self.transcriber(audio, self.cfg.whisper_model, self.cfg.model_timeout_s).text
            except (video.VideoError, models.ModelError, tools.ToolError) as exc:
                ev.notes.append(f"speech transcription failed: {type(exc).__name__}")
            finally:
                (work / "audio.wav").unlink(missing_ok=True)
        return ev

    def evidence(self, kind: str, path: Path, work: Path) -> Evidence:
        """Gather evidence once per job and cache it in the job's temp folder (rewrites reuse it)."""
        cache = work / "evidence.json"
        if cache.exists():
            return Evidence(**json.loads(cache.read_text(encoding="utf-8")))
        work.mkdir(parents=True, exist_ok=True)
        ev = self.picture_evidence(path, work) if kind == "picture" else self.video_evidence(path, work)
        if not any("failed" in note for note in ev.notes):  # a model or tool failure is retried next time
            cache.write_text(json.dumps(ev.__dict__), encoding="utf-8")
        return ev

    # -- captions -------------------------------------------------------------------------------
    def _meta(self, kind: str) -> dict:
        meta = {"vision_model": self.cfg.vision_model, "text_model": self.cfg.text_model}
        if kind == "video" and self.whisper_available:
            meta["whisper_model"] = self.cfg.whisper_model
        return meta

    def _generate(self, prompt: str, transcript: str) -> str | None:
        system = models.system_prompt(self.cfg.language)
        for attempt in range(2):  # one retry, with a different seed, if the output breaks the rules
            try:
                raw = self.ollama.chat(self.cfg.text_model, prompt, system=system, seed=7 + attempt)
            except models.ModelUnreachableError as exc:
                raise CaptionUnavailableError(str(exc)) from exc
            except models.ModelError:
                return None
            caption = models.validate_caption(raw, transcript)
            if caption:
                return caption
        return None

    @staticmethod
    def _fallback(kind: str, ev: Evidence) -> CaptionResult:
        if ev.date_text:
            noun = "photo taken" if kind == "picture" else "video recorded"
            return CaptionResult(f"A {noun} on {ev.date_text}.\nNo automatic description was available.",
                                 "metadata", {}, "; ".join(ev.notes))
        return CaptionResult(None, None, {}, "; ".join(ev.notes) or "not enough evidence for a caption")

    def caption(self, kind: str, path: Path, work: Path) -> CaptionResult:
        ev = self.evidence(kind, path, work)
        if not ev.descriptions and not ev.transcript:
            return self._fallback(kind, ev)
        caption = self._generate(models.caption_prompt(ev.block(), kind), ev.transcript)
        if caption is None:
            return self._fallback(kind, ev)
        return CaptionResult(caption, "generated", self._meta(kind), "; ".join(ev.notes))

    def rewrite(self, kind: str, path: Path, work: Path, current: str | None, instruction: str) -> CaptionResult:
        ev = self.evidence(kind, path, work)
        if not ev.descriptions and not ev.transcript and not ev.date_text:
            return CaptionResult(current, None, {}, "no evidence to rewrite from; reply with your own words")
        prompt = models.rewrite_prompt(current or "(no caption yet)", instruction, ev.block())
        caption = self._generate(prompt, ev.transcript)
        if caption is None:
            return CaptionResult(current, None, {}, "rewrite did not produce a valid caption")
        return CaptionResult(caption, "generated", self._meta(kind), "")
