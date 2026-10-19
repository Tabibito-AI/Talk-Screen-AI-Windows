import asyncio
import sys
import traceback
from typing import Optional
import os
import json
import time
import ctypes
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image
import textwrap
import pyttsx3
import numpy as np
import pythoncom
import io
import wave
import random
import re

from google import genai
from google.genai import types

def to_markdown(text):
    text = text.replace('•', '  *')
    return textwrap.indent(text, '    ')

# Add src directory to Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.config.settings import config
from src.logger import logger
from src.audio.audio_handler import AudioHandler
from src.camera.camera_capture_windows import CameraCaptureWindows
from src.screen.screen_capture_windows import grab_screen_image

REPLY_STYLES = (
    "warm and conversational",
    "crisp and practical",
    "curious and collaborative",
    "calm and reassuring",
    "energetic and encouraging",
    "thoughtful and analytical",
    "friendly and succinct",
    "playful but professional",
    "reflective and supportive",
    "focused and action-oriented",
    "gently challenging",
    "optimistic and forward-looking",
    "clear and no-nonsense",
    "patient and explanatory",
    "lightly humorous when appropriate",
    "strategic and structured",
    "empathetic and grounding",
    "creative and idea-generating",
    "efficient and task-focused",
    "mentor-like and constructive",
    "casual and natural",
    "confident and concise",
    "research-partner oriented",
    "reflective and question-led",
    "quietly motivating",
    "solution-first",
    "big-picture then practical",
    "detail-aware and precise",
    "friendly and curious",
    "decisive and momentum-building",
)

# (profile category the answer would fill, what to ask about).
# Deliberately NOT task material: the point of a question is to get to know the person,
# never to extract a schedule or a paper plan. "appearance" is absent on purpose - that
# is something you notice quietly from the webcam, never something you interview them on.
FOLLOW_UP_THEMES = (
    ("identity", "what they would like you to call them"),
    ("identity", "whether you are saying their name the way they say it"),
    ("personal", "what they are studying, in plain everyday words"),
    ("personal", "how long they have been in this room or this programme"),
    ("personal", "which language they are most comfortable in"),
    ("personal", "whether they are more of a morning or a night person"),
    ("personal", "which part of their work they actually enjoy"),
    ("research", "what first drew them to their field"),
    ("research", "the kind of question they find genuinely interesting"),
    ("research", "whether they like reading, thinking, data, or writing best"),
    ("research", "whether they prefer working alone or alongside other people"),
    ("research", "a researcher or an idea they admire"),
    ("hobbies", "what they do when they are not working"),
    ("hobbies", "music they like"),
    ("hobbies", "whether they play or watch any sport"),
    ("hobbies", "games, films, or shows they are into"),
    ("hobbies", "something they read for fun"),
    ("hobbies", "something they have been meaning to try"),
    ("life", "what they like to eat"),
    ("life", "how they get here in the morning"),
    ("life", "where they are from"),
    ("life", "whether family or friends are nearby"),
    ("life", "how they like to spend a weekend"),
    ("life", "a place they would like to travel to"),
    ("life", "whether they have a pet, or would like one"),
    ("life", "how they wind down at the end of the day"),
    ("life", "what the weather or the season is doing to their mood"),
    ("room", "what else is in this room"),
    ("room", "whether they share the room with other people"),
    ("room", "whether it is usually quiet or noisy in here"),
    ("room", "something on their desk they are fond of"),
    ("room", "whether the light or the temperature in here suits them"),
)

# The long-term profile: the categories the user asked to be remembered, in the order
# they are shown to the model. Caps are per category so a chatty week of one kind of
# fact cannot evict another kind - identity in particular must never fall out.
PROFILE_CATEGORIES = (
    ("identity", "WHO THEY ARE"),
    ("research", "What they are interested in (general)"),
    ("personal", "General things about them"),
    ("hobbies", "Hobbies and what they enjoy"),
    ("life", "Life outside work"),
    ("room", "The room they sit in"),
    ("appearance", "What they look like"),
)
PROFILE_CATEGORY_CAPS = {
    "identity": 5,
    "research": 6,
    "personal": 6,
    "hobbies": 6,
    "life": 6,
    "room": 5,
    "appearance": 3,
}
ASKABLE_CATEGORIES = frozenset(
    {"identity", "personal", "research", "hobbies", "life", "room"}
)
DEFAULT_PROFILE_CATEGORY = "personal"
PROFILE_RECORD_CHARS = 140   # per rendered fact
PROFILE_BLOCK_CHARS = 1800   # whole profile block; identity is exempt

# How the assistant may open a turn, so it does not fall into one stock greeting.
OPENING_MOVES = (
    "answer immediately with no preamble",
    "react briefly to what they said, then answer",
    "open with a short observation about the room or how they look, then answer",
    "acknowledge the feeling behind what they said, then answer",
    "pick up a thread from an earlier conversation, then answer",
    "start with the single most useful fact, then add context",
)

# Conversation shapes, so not every reply is "answer + follow-up question".
REPLY_SHAPES = (
    "a complete answer with NO question at the end",
    "a complete answer with NO question at the end",
    "just two or three sentences, conversational, no question",
    "a short answer, then ONE easy, friendly question",
    "a brief observation plus a small suggestion, no question",
    "a direct answer and then a light comment about the moment",
    "a short answer plus ONE simple question about them as a person",
)

if sys.version_info < (3, 11, 0):
    import taskgroup, exceptiongroup
    asyncio.TaskGroup = taskgroup.TaskGroup
    asyncio.ExceptionGroup = exceptiongroup.ExceptionGroup

class TalkScreenAI:
    """音声チャットとカメラ観察を組み合わせた常駐AIアシスタント (Windows版)"""

    def __init__(self):
        """TalkScreenAIインスタンスを初期化"""
        self.audio_handler = AudioHandler(config.audio)
        self.is_running = True
        self.workspace_root = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.memory_file = self.workspace_root / "assistant_memory.json"

        # Gemini APIクライアントの初期化（google-genai SDK）
        self.client = genai.Client(api_key=config.api_key)

        # キューの初期化
        self.audio_out_queue: Optional[asyncio.Queue] = None
        self.camera_capture: Optional[CameraCaptureWindows] = None
        self.stop_event: Optional[asyncio.Event] = None
        self.session = None

        # API health. Quota exhaustion pauses calls for a while instead of forever,
        # so a temporary limit does not brick the assistant until restart.
        self.api_blocked_until = 0.0
        self.api_backoff_sec = float(os.getenv("API_BACKOFF_SEC", "120"))
        self._quota_notice_spoken = False

        self.is_speaking = False
        self.tts_lock = asyncio.Lock()
        self.proactive_lock = asyncio.Lock()

        # Voice activity detection.
        self.speech_rms_threshold = int(os.getenv("SPEECH_RMS_THRESHOLD", "220"))
        self.speech_rms_margin = int(os.getenv("SPEECH_RMS_MARGIN", "150"))
        self.noise_floor_rms = float(self.speech_rms_threshold) * 0.5
        self.noise_floor_alpha = 0.05
        # Timbre gates: loudness alone cannot tell a spoken word from a door slam.
        self.voice_band_ratio_min = float(os.getenv("VOICE_BAND_RATIO_MIN", "0.55"))
        self.voice_flatness_max = float(os.getenv("VOICE_FLATNESS_MAX", "0.40"))
        self.voice_zcr_max = float(os.getenv("VOICE_ZCR_MAX", "0.30"))
        # Movement gates, measured against real speech vs tonal noise. Speech's worst
        # 1s slice scored flux 0.70 / level-variation 0.42; the loudest offenders scored
        # flux 0.23 (fan whine) and variation 0.21 (tremolo chord), so both must pass.
        self.voice_min_flux = float(os.getenv("VOICE_MIN_FLUX", "0.40"))
        self.voice_min_level_cov = float(os.getenv("VOICE_MIN_LEVEL_COV", "0.25"))
        # Kept deliberately low. Noise rejection is the timbre and movement gates' job;
        # making these strict instead just deafens the assistant to short words. Measured
        # on real speech: "Hi"/"Yes" carry only 3 voiced chunks and "Thanks" just 2, so a
        # 3-chunk attack never even starts on a word beginning with a fricative.
        self.voice_attack_chunks = max(1, int(os.getenv("VOICE_ATTACK_CHUNKS", "1")))
        self.voice_silence_chunks = int(os.getenv("VOICE_SILENCE_CHUNKS", "8"))
        # Counts only chunks that actually carried voice, so the silence tail cannot
        # make a cough look like a sentence. Must stay ABOVE voice_attack_chunks, or the
        # proactive "heard someone but they didn't address me" branch becomes unreachable:
        # voiced_chunks starts at the attack count.
        self.voice_min_voiced_chunks = max(
            self.voice_attack_chunks + 1, int(os.getenv("VOICE_MIN_VOICED_CHUNKS", "2"))
        )
        self.voice_max_seconds = int(os.getenv("VOICE_MAX_SECONDS", "15"))

        # Self-trigger prevention: the assistant must never answer its own voice.
        self.tts_tail_guard_sec = float(os.getenv("TTS_TAIL_GUARD_SEC", "0.4"))
        self._mic_drain_pending = False

        # Proactive conversation.
        self.proactive_cooldown_sec = int(os.getenv("PROACTIVE_COOLDOWN_SEC", "240"))
        self.proactive_min_idle_sec = int(os.getenv("PROACTIVE_MIN_IDLE_SEC", "150"))
        self.last_proactive_at = float("-inf")
        self.last_interaction_at = float("-inf")
        # When the assistant last ended a spoken line with a question: a short answer
        # ("yes", "はい") right after that is a real reply, not a fragment.
        self.last_question_at = float("-inf")
        self.pending_turns = 0
        self._proactive_task: Optional[asyncio.Task] = None

        # Screen sharing: captured only for the turns that actually need it.
        self.screen_monitor = int(os.getenv("SCREEN_MONITOR", "1"))
        self.screen_max_edge = int(os.getenv("SCREEN_MAX_EDGE", "1280"))
        self.screen_capture_enabled = os.getenv("SCREEN_CAPTURE_ENABLED", "1") != "0"

        # Speech synthesis. tts_languages is filled in at startup by _detect_tts_voices();
        # a reply in a language no installed voice speaks comes out as pure silence.
        self.tts_rate = int(os.getenv("TTS_RATE", "185"))
        self.tts_voice_hint = os.getenv("TTS_VOICE_NAME", "").strip().lower()
        self.tts_languages: set = set()
        self.tts_voice_by_language: dict = {}
        self.reply_language_rule = ""

        # Memory. Caps are per profile category (PROFILE_CATEGORY_CAPS) and the prompt
        # block is bounded by characters, so max_key_points is only an outer backstop.
        self.max_observations = int(os.getenv("MAX_ENV_OBSERVATIONS", "60"))
        self.max_key_points = int(os.getenv("MAX_CONVERSATION_KEY_POINTS", "120"))
        self.max_visual_notes = int(os.getenv("MAX_VISUAL_NOTES", "12"))
        self._pending_memory_payload: Optional[str] = None
        self._memory_save_task: Optional[asyncio.Task] = None

        # Only an OPERATOR-supplied prompt is injected. The settings.py fallback
        # ("professional and detailed ... as thorough an answer as possible") directly
        # contradicts every brevity rule in the persona below, and nothing sets
        # SYSTEM_PROMPT in .env, so it would otherwise apply on every turn.
        self.operator_system_prompt = os.getenv("SYSTEM_PROMPT", "").strip()

        self.recent_reply_styles: list[str] = []
        self.recent_follow_up_themes: list[str] = []
        self.dynamic_system_prompt = ""   # built by _refresh_dynamic_system_prompt below
        self.latest_room_context = "No room context collected yet."
        self.memory_data = {
            "assistant_profile": {
                "name": "RoomMate",
                "mission": (
                    "Share the room with one person, get to know them as a person, "
                    "and be good company."
                ),
            },
            "observations": [],
            "key_points": [],
            "visual_notes": [],
        }
        self._load_memory()
        self._refresh_dynamic_system_prompt()

    # ------------------------------------------------------------------ memory

    def _load_memory(self) -> None:
        """永続メモリを読み込み、前回までの観察を復元する"""
        try:
            if self.memory_file.exists():
                loaded = json.loads(self.memory_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.memory_data.update(loaded)
        except Exception as e:
            logger.error(f"永続メモリの読み込みに失敗しました: {e}")

        self.memory_data.setdefault("observations", [])
        self.memory_data.setdefault("key_points", [])
        self.memory_data.setdefault("visual_notes", [])
        self._prune_memory()

    def _prune_memory(self) -> None:
        """Drop stale screen-capture-era observations and bound every list.

        The old build stored whole screen-analysis essays as observations. Left alone
        they dominate the prompt and push the assistant toward stale, generic replies.
        """
        observations = [
            item for item in self.memory_data.get("observations", [])
            if isinstance(item, dict) and item.get("kind") != "environment"
        ]
        self.memory_data["observations"] = observations[-self.max_observations:]
        self.memory_data["key_points"] = self._cap_key_points([
            item for item in self.memory_data.get("key_points", [])
            if isinstance(item, dict) and not self._is_junk_fact(str(item.get("content", "")))
        ])
        self.memory_data["visual_notes"] = [
            item for item in self.memory_data.get("visual_notes", []) if isinstance(item, dict)
        ][-self.max_visual_notes:]

    # Discourse markers that are never a message on their own, in either language.
    # "うん"/"はい"/"yes" are deliberately ABSENT: those are real answers when the
    # assistant has just asked something, and context is the model's job to judge.
    _FILLER_TOKENS = frozenset(
        """
        uh um umm ah ahh eh ehh hm hmm mm mmm mhm oh ooh er err huh ha well so anyway okay ok
        m u a e o h uhm erm ahem aha ugh meh hmph
        あ あっ あー ああ ぁ え えっ えー ええと えーと えっと あの あのー あのう
        ん んー んん んーん まあ まー ま じゃ じゃあ じゃー では で そう そうそう そっか
        へえ へー ふーん ほう おお お わ うわ ねえ ね さて よし とりあえず
        """.split()
    )

    @classmethod
    def _is_filler_token(cls, seg: str) -> bool:
        """Filler, including stretched forms: "ummmm", "ahhhh", "hmmmm", "えーーっと"."""
        if seg in cls._FILLER_TOKENS:
            return True
        # Collapse every run of a repeated character ("ummmm" -> "um"), with and without
        # long-vowel marks ("えーーっと" -> "えっと").
        collapsed = re.sub(r"(.)\1+", r"\1", seg).rstrip("ー〜")
        no_marks = re.sub(r"(.)\1+", r"\1", re.sub(r"[ー〜]", "", seg))
        return collapsed in cls._FILLER_TOKENS or no_marks in cls._FILLER_TOKENS

    @classmethod
    def _is_meaningful_transcript(cls, text: str, short_answer_ok: bool = False) -> bool:
        """True only for a clear sentence worth answering out loud.

        Blocks noise the model transcribed into a fragment, and thinking-aloud filler
        ("ah", "ummm"). Filler segments are dropped first, and what remains must be a
        real sentence: 2+ words, or 4+ kana/kanji for Japanese (which has no spaces).
        A one-word answer ("yes", "はい", "Python") only counts when short_answer_ok -
        i.e. the assistant has just asked a question.
        """
        stripped = (text or "").strip()
        if len(stripped) < 2:
            return False
        # Letters in Latin, kana, or CJK - punctuation and "..." alone are not speech.
        letters = re.findall(r"[a-zA-Z぀-ゟ゠-ヿ一-鿿]", stripped)
        if len(letters) < 2:
            return False

        segments = [
            seg.strip().lower().rstrip("ー〜")
            for seg in re.split(r"[\s,.!?;:、。！？…'\"()\[\]-]+", stripped)
            if seg.strip()
        ]
        content = [seg for seg in segments if not cls._is_filler_token(seg)]
        if not content:
            return False
        if short_answer_ok:
            return True
        latin_words = [seg for seg in content if re.search(r"[a-z]", seg)]
        cjk_chars = len(re.findall(r"[぀-ゟ゠-ヿ一-鿿]", "".join(content)))
        return len(latin_words) >= 2 or cjk_chars >= 4 or (bool(latin_words) and cjk_chars >= 2)

    # Volatile, task-shaped material. The assistant is a room-mate, not a project
    # manager: it remembers WHO SOMEONE IS, never their calendar. All of this goes stale
    # within a day, and storing it is what made the assistant nag about outlines and
    # meetings. Applied on write AND on load (via _prune_memory), so entries already on
    # disk scrub themselves at startup.
    # Deliberately NOT applied to observations, visual_notes, or the spoken reply: the
    # assistant may still discuss a deadline in the moment, it just never writes it down.
    _VOLATILE_FACT_PATTERN = re.compile(
        # clock and calendar
        r"\b(?:to(?:day|morrow|night)|yesterday|this (?:morning|afternoon|evening|week|month)"
        r"|next (?:week|month|year|mon|tues|wednes|thurs|fri|satur|sun)day"
        r"|(?:mon|tues|wednes|thurs|fri|satur|sun)day|weekday"
        r"|january|february|march|april|june|july|august|september|october|november|december"
        r"|jan|feb|apr|jun|jul|aug|oct|nov|dec)\b"
        r"|\b\d{1,2}\s*[:.]\s*\d{2}\b|\b\d{1,2}\s*(?:am|pm|a\.m\.|p\.m\.)\b|\bo'?clock\b"
        r"|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}\b"
        # calendar and task vocabulary
        r"|\b(?:deadline|due date|schedul\w+|appointment|agenda|reminder|remind\w*|timer|alarm"
        r"|meetings?|seminar|lecture|milestone|to-?do|tasks?|checklist|submission|submit\w*)\b"
        # paper and writing mechanics
        r"|\b(?:outline|table of contents|chapter|subsection|draft\w*|manuscript|revision"
        r"|abstract|literature|papers?|thesis)\b"
        # plan-shaped phrasing
        r"|\b(?:plans? to|planning to|intends? to|aims? to|by the end of|next step"
        r"|will (?:start|begin|read|write|draft|prepare))\b"
        # requests aimed at the assistant - never a fact about the person
        r"|\bplease\b|\blog ?(?:off|out)\b|\bshut ?down\b|\bprint\w*\b"
        r"|\block (?:the |my |his |her |their )?(?:screen|workstation|computer|pc|laptop)\b"
        r"|\bset (?:a |an )?(?:timer|alarm|reminder)\b"
        # Japanese equivalents (no \b in CJK, so these are bare substrings)
        r"|明日|明後日|今日|本日|昨日|今週|来週|今夜|今朝|午前|午後|時から|時まで"
        r"|締め切り|締切|期限|予定|会議|打ち合わせ|ミーティング|セミナー|面談|発表"
        r"|論文|アウトライン|目次|原稿|草稿|タスク|リマインド|タイマー|印刷",
        flags=re.IGNORECASE,
    )

    @classmethod
    def _is_junk_fact(cls, content: str) -> bool:
        """True for anything that must not become a durable fact about the person.

        Two families. (1) The user's own questions - an earlier build swept any sentence
        containing a keyword into memory, so "Could you help me plan tomorrow?" was
        stored as a fact and replayed into every prompt forever. (2) Volatile task
        material - dates, deadlines, meetings, and paper mechanics.

        Over-rejection is the intended direction: a missed hobby is re-offered next turn,
        whereas a missed deadline pollutes every prompt until it is hand-deleted.
        """
        body = " ".join(content.split())
        for prefix in ("User said:", "User asked:"):
            if body.startswith(prefix):
                body = body[len(prefix):].strip()
        if not body:
            return True
        if body.endswith("?"):
            return True
        if re.match(
            r"^(can|could|would|will|do|does|did|is|are|how|what|why|when|where|who|"
            r"should|shall|may|might|please)\b",
            body,
            flags=re.IGNORECASE,
        ):
            return True
        return bool(cls._VOLATILE_FACT_PATTERN.search(body))

    @staticmethod
    def _utc_stamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _write_memory_payload(self, payload: str) -> None:
        try:
            self.memory_file.write_text(payload, encoding="utf-8")
        except Exception as e:
            logger.error(f"永続メモリの保存に失敗しました: {e}")

    def _save_memory(self) -> None:
        """Queue a debounced write so disk I/O never blocks the event loop."""
        try:
            self._pending_memory_payload = json.dumps(
                self.memory_data, ensure_ascii=False, indent=2
            )
        except Exception as e:
            logger.error(f"永続メモリのシリアライズに失敗しました: {e}")
            return

        if self._memory_save_task is None or self._memory_save_task.done():
            try:
                self._memory_save_task = asyncio.get_running_loop().create_task(
                    self._flush_memory()
                )
            except RuntimeError:
                # No loop yet (startup): write straight through.
                payload, self._pending_memory_payload = self._pending_memory_payload, None
                if payload is not None:
                    self._write_memory_payload(payload)

    async def _flush_memory(self) -> None:
        """Coalesce bursts of memory updates into a single off-loop write."""
        while True:
            await asyncio.sleep(2.0)
            payload, self._pending_memory_payload = self._pending_memory_payload, None
            if payload is None:
                return
            await asyncio.to_thread(self._write_memory_payload, payload)

    @staticmethod
    def _fact_key(content: str) -> str:
        """Dedupe key: casefolded, punctuation- and space-insensitive."""
        return re.sub(r"[^a-z0-9぀-ゟ゠-ヿ一-鿿]+", "", str(content).casefold())

    def _cap_key_points(self, records: list) -> list:
        """Bound each profile category independently, then restore chronological order.

        A single global tail-truncation let a chatty week evict the user's own name.
        Identity additionally pins its OLDEST record, so the name survives any amount of
        churn. Chronological order is restored afterwards so that when two notes
        disagree, the later one is read last and therefore wins.
        """
        buckets: dict = {}
        for item in records:
            if not isinstance(item, dict):
                continue
            category = str(item.get("category", "") or "").strip().lower()
            if category not in PROFILE_CATEGORY_CAPS:
                category = DEFAULT_PROFILE_CATEGORY
            buckets.setdefault(category, []).append(item)

        out: list = []
        for category, items in buckets.items():
            cap = PROFILE_CATEGORY_CAPS[category]
            if len(items) > cap:
                if category == "identity":
                    items = items[:1] + items[-(cap - 1):]
                else:
                    items = items[-cap:]
            out.extend(items)
        out.sort(key=lambda item: str(item.get("ts", "")))
        return out[-self.max_key_points:]

    def _remember_facts(self, facts: list) -> None:
        """Store durable, model-selected facts about the PERSON, filed by category.

        The model returns these in the same call that produces the reply, so this costs
        no extra request. Entries may be {"category", "content"} or a bare string; an
        unknown or missing category falls back to 'personal' rather than being dropped,
        so a model that regresses to plain strings still works.
        """
        memories = self.memory_data.setdefault("key_points", [])
        known = {
            self._fact_key(item.get("content", ""))
            for item in memories if isinstance(item, dict)
        }
        added = False
        for fact in facts:
            if isinstance(fact, dict):
                raw = fact.get("content", fact.get("fact", ""))
                category = str(fact.get("category", "") or "").strip().lower()
            else:
                raw, category = fact, ""
            cleaned = " ".join(str(raw).split())[:240]
            if len(cleaned) < 4:
                continue
            if self._is_junk_fact(cleaned):
                logger.info("Not remembering volatile/non-fact material: %r", cleaned[:90])
                continue
            if category not in PROFILE_CATEGORY_CAPS:
                category = DEFAULT_PROFILE_CATEGORY
            key = self._fact_key(cleaned)
            if key in known:
                continue
            memories.append(
                {"ts": self._utc_stamp(), "category": category, "content": cleaned}
            )
            known.add(key)
            added = True
        if added:
            self.memory_data["key_points"] = self._cap_key_points(memories)

    def _remember_visual_note(self, note: str) -> None:
        cleaned = " ".join(str(note).split())[:280]
        if not cleaned:
            return
        notes = self.memory_data.setdefault("visual_notes", [])
        if notes and str(notes[-1].get("content", "")).casefold() == cleaned.casefold():
            return
        notes.append({"ts": self._utc_stamp(), "content": cleaned})
        if len(notes) > self.max_visual_notes:
            self.memory_data["visual_notes"] = notes[-self.max_visual_notes:]
        self.latest_room_context = cleaned

    def _remember_observation(self, kind: str, content: str) -> None:
        observations = self.memory_data.setdefault("observations", [])
        observations.append({"ts": self._utc_stamp(), "kind": kind, "content": content[:800]})
        if len(observations) > self.max_observations:
            self.memory_data["observations"] = observations[-self.max_observations:]

    def _recent_dialog_digest(self) -> str:
        """A few recent exchanges so replies can refer back naturally."""
        recent = [
            item for item in self.memory_data.get("observations", [])
            if isinstance(item, dict)
            and item.get("kind") in {"dialog", "voice_dialog", "proactive"}
        ][-4:]
        if not recent:
            return "- Nothing discussed yet in this room."
        return "\n".join(f"- {str(item.get('content', ''))[:220]}" for item in recent)

    @classmethod
    def _group_profile(cls, key_points) -> dict:
        """Facts bucketed by category, oldest first.

        Legacy records written before the profile existed have no category; they land in
        'personal' rather than being lost. content is coerced with str() because a
        hand-edited file could hold a non-string, which used to crash the renderer -
        and therefore __init__ - with a bare TypeError.
        """
        grouped = {name: [] for name, _ in PROFILE_CATEGORIES}
        for item in key_points:
            if not isinstance(item, dict):
                continue
            content = " ".join(str(item.get("content", "")).split())
            if not content:
                continue
            category = str(item.get("category", "") or "").strip().lower()
            if category not in grouped:
                category = DEFAULT_PROFILE_CATEGORY
            grouped[category].append(content)
        return grouped

    @classmethod
    def _render_profile_block(cls, key_points) -> tuple:
        """(profile text, categories still unknown).

        Identity is emitted first and is exempt from the character budget: the assistant
        must never lose the person's name because it learned a lot about their hobbies.
        """
        grouped = cls._group_profile(key_points)
        sections, used = [], 0
        for name, heading in PROFILE_CATEGORIES:
            entries = grouped.get(name, [])[-PROFILE_CATEGORY_CAPS[name]:]
            if not entries:
                continue
            text = "\n".join(
                [f"{heading}:"] + [f"- {e[:PROFILE_RECORD_CHARS]}" for e in entries]
            )
            if name != "identity" and used + len(text) > PROFILE_BLOCK_CHARS:
                continue
            sections.append(text)
            used += len(text)
        unknown = [
            name for name, _ in PROFILE_CATEGORIES
            if name in ASKABLE_CATEGORIES and not grouped.get(name)
        ]
        return (
            "\n\n".join(sections) or "You have not learned anything about them yet.",
            unknown,
        )

    def _refresh_dynamic_system_prompt(self) -> None:
        profile_block, unknown = self._render_profile_block(
            self.memory_data.get("key_points", [])
        )
        if unknown:
            curiosity = (
                "You still know nothing about: " + ", ".join(unknown) + ". "
                "If you ask anything at all this turn, ask about ONE of those."
            )
        else:
            curiosity = (
                "You already know something in every area. You do not need to ask "
                "get-to-know-you questions any more - just talk with them."
            )

        visual_notes = self.memory_data.get("visual_notes", [])[-3:]
        visual_history = "\n".join(
            f"- {str(item.get('content', ''))[:160]}"
            for item in visual_notes if isinstance(item, dict)
        ) or "- Nothing seen yet."

        operator_block = (
            f"Extra instructions from the operator:\n{self.operator_system_prompt}\n\n"
            if self.operator_system_prompt else ""
        )

        self.dynamic_system_prompt = (
            "You share a room with one person. You can hear them through a microphone "
            "and see them through a webcam. Think of yourself as a friendly room-mate, "
            "not an assistant with a clipboard: over weeks and months your job is simply "
            "to get to know them as a person and be good company. Be warm, natural, and "
            "brief - you are speaking out loud, not writing an essay.\n\n"
            "HARD RULES:\n"
            "- You CANNOT control the computer. You cannot lock the screen, open apps, send "
            "email, set timers, or change files. Never claim you did any of these. If asked, "
            "say plainly that you cannot do it, and offer what you can do instead.\n"
            "- You are NOT a project manager and you do NOT keep their calendar. Never "
            "recite, track, remind, or ask about dates, times, meetings, seminars, "
            "deadlines, submissions, to-do lists, or the structure of a paper - no "
            "outlines, chapters, or drafts. If they ask what is on their schedule, say "
            "plainly that you do not keep track of it, and offer to look at their screen "
            "instead, where their calendar probably is.\n"
            "- Never invent facts about the user. Use only what they told you or what you "
            "can clearly see.\n"
            "- Use their name only if it appears in the profile below, and spell it EXACTLY "
            "as recorded there. Never guess, lengthen, or re-spell a name. Using it once in "
            "a while is warmer than using it every turn.\n"
            "- If two profile notes disagree, the LATER one is correct.\n"
            "- Get to know them slowly, the way a person would. At most ONE light question "
            "per reply and often none at all. Never two questions in one turn, never a run "
            "of questions across turns, and never ask something the profile below already "
            "answers. If they do not want to talk, let it go.\n"
            "- Keep spoken replies under about 60 words unless they asked for detail.\n"
            f"{self.reply_language_rule}"
            "\n"
            f"{operator_block}"
            "WHAT YOU KNOW ABOUT THEM (their long-term profile):\n"
            f"{profile_block}\n\n"
            f"{curiosity}\n\n"
            "What you have seen in the room recently (oldest first):\n"
            f"{visual_history}\n\n"
            "Recent conversation:\n"
            f"{self._recent_dialog_digest()}"
        )

    def _compose_prompt(self, task_instruction: str) -> str:
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return (
            f"{self.dynamic_system_prompt}\n\n"
            f"Current local time: {now_text}\n"
            f"Most recent thing you saw: {self.latest_room_context}\n\n"
            f"Task: {task_instruction}"
        )

    # ------------------------------------------------------------- variety

    def _next_reply_style(self) -> str:
        available = [style for style in REPLY_STYLES if style not in self.recent_reply_styles]
        style = random.choice(available or list(REPLY_STYLES))
        self.recent_reply_styles.append(style)
        self.recent_reply_styles = self.recent_reply_styles[-8:]
        return style

    def _next_follow_up_theme(self) -> str:
        """Prefer a question that fills a THIN part of the profile.

        Choosing uniformly at random meant re-asking things it already knew. The profile
        is the source of truth: a category with two or more facts counts as known enough
        and drops out of the pool until the thin ones are exhausted.
        """
        grouped = self._group_profile(self.memory_data.get("key_points", []))
        fresh = [t for t in FOLLOW_UP_THEMES if t[1] not in self.recent_follow_up_themes]
        thin = [t for t in fresh if len(grouped.get(t[0], ())) < 2]
        theme = random.choice(thin or fresh or list(FOLLOW_UP_THEMES))[1]
        self.recent_follow_up_themes.append(theme)
        self.recent_follow_up_themes = self.recent_follow_up_themes[-20:]
        return theme

    def _variation_directives(self) -> str:
        """Randomised shape/opening/tone so consecutive replies do not rhyme."""
        return (
            f"Tone for this turn: {self._next_reply_style()}. "
            f"Open like this: {random.choice(OPENING_MOVES)}. "
            f"Shape this reply as: {random.choice(REPLY_SHAPES)}. "
            f"If - and only if - you ask something, ask ONE short, easy question about "
            f"{self._next_follow_up_theme()}, the way a room-mate would over coffee - not "
            "an interviewer. One question maximum, nothing about their schedule or their "
            "paper, and nothing the profile already answers. "
            "Vary your wording from previous turns; never reuse a stock greeting or a stock "
            "closing question."
        )

    # --------------------------------------------------------------- windows

    def _enable_keep_awake_mode(self) -> None:
        """画面オフ中も処理が継続するよう、スリープ移行を抑制

        SetThreadExecutionState is thread-scoped, so this must run on the main thread
        that stays alive for the process lifetime (it does: run() owns the event loop).
        ES_DISPLAY_REQUIRED is deliberately omitted - the user wants the assistant awake
        while the screen is off.
        """
        if sys.platform != "win32":
            return
        try:
            es_continuous = 0x80000000
            es_system_required = 0x00000001
            es_awaymode_required = 0x00000040
            ctypes.windll.kernel32.SetThreadExecutionState(
                es_continuous | es_system_required | es_awaymode_required
            )
            logger.info("常時稼働モードを有効化しました（画面オフ時も継続）")
        except Exception as e:
            logger.error(f"常時稼働モードの有効化に失敗しました: {e}")

    def _disable_keep_awake_mode(self) -> None:
        if sys.platform != "win32":
            return
        try:
            es_continuous = 0x80000000
            ctypes.windll.kernel32.SetThreadExecutionState(es_continuous)
        except Exception:
            pass

    # ------------------------------------------------------------------ audio

    def _voice_timbre_features(self, pcm: np.ndarray, sample_rate: int) -> tuple:
        """(voice-band energy ratio, spectral flatness, zero-crossing rate).

        These separate a human voice from the noises that used to trigger the assistant:
        - band ratio: speech puts most of its energy in ~200-3500 Hz. A door thud is
          nearly all sub-200 Hz; hiss and key clicks spread far above 3500 Hz.
        - flatness: voiced speech is harmonic, so its spectrum is peaky (low flatness).
          White-ish noise (fans, rustling, static) is flat, approaching 1.0.
        - zero-crossing rate: clicks and hiss cross zero far more often than speech.
        """
        x = pcm.astype(np.float32)
        if x.size < 64:
            return 0.0, 1.0, 1.0

        spectrum = np.abs(np.fft.rfft(x * np.hanning(x.size)))
        freqs = np.fft.rfftfreq(x.size, 1.0 / sample_rate)
        total = float(spectrum.sum()) + 1e-9
        band_ratio = float(spectrum[(freqs >= 200.0) & (freqs <= 3500.0)].sum()) / total

        power = spectrum.astype(np.float64) ** 2 + 1e-12
        flatness = float(np.exp(np.mean(np.log(power))) / np.mean(power))

        zcr = float(np.mean(np.abs(np.diff(np.sign(x)))) / 2.0)
        return band_ratio, flatness, zcr

    def _utterance_modulation(self, pcm: np.ndarray, sample_rate: int) -> tuple:
        """(spectral flux, RMS coefficient of variation) over a whole utterance.

        Timbre alone cannot reject a sustained chord, an alarm tone, or a fan whine:
        those are harmonic and sit in the voice band, so they look like a vowel. What
        they lack is speech's constant movement - syllables make both the loudness and
        the spectrum fluctuate several times a second, while a held tone is stationary.
        """
        frame = 1024
        frames = [
            pcm[i:i + frame] for i in range(0, len(pcm) - frame + 1, frame)
        ]
        if len(frames) < 3:
            return 0.0, 0.0

        spectra, levels = [], []
        for f in frames:
            x = f.astype(np.float32)
            spectrum = np.abs(np.fft.rfft(x * np.hanning(x.size)))
            spectra.append(spectrum / (spectrum.sum() + 1e-9))
            levels.append(float(np.sqrt(np.mean(x ** 2))))

        flux = float(np.mean(np.abs(np.diff(np.array(spectra), axis=0)).sum(axis=1)))
        levels = np.array(levels)
        cov = float(np.std(levels) / (np.mean(levels) + 1e-9))
        return flux, cov

    def _utterance_is_speech_like(self, pcm: np.ndarray, sample_rate: int) -> bool:
        flux, cov = self._utterance_modulation(pcm, sample_rate)
        return flux >= self.voice_min_flux and cov >= self.voice_min_level_cov

    def _is_voice_like(self, pcm: np.ndarray, sample_rate: int) -> bool:
        band_ratio, flatness, zcr = self._voice_timbre_features(pcm, sample_rate)
        return (
            band_ratio >= self.voice_band_ratio_min
            and flatness <= self.voice_flatness_max
            and zcr <= self.voice_zcr_max
        )

    def _is_speech_chunk(self, audio_data: dict) -> bool:
        """Classify a chunk as human speech: loud enough AND shaped like a voice.

        A fixed RMS threshold false-triggers on steady ambient noise (fans, hum) once the
        room gets loud enough. Tracking the noise floor and requiring a margin above it keeps
        detection tied to the room's actual baseline. But loudness alone still cannot tell a
        spoken word from a door slam or a keyboard, so the chunk must also pass the timbre
        gates before it counts as speech.
        """
        try:
            pcm_bytes = audio_data.get("data", b"")
            if not pcm_bytes:
                return False
            pcm = np.frombuffer(pcm_bytes, dtype=np.int16)
            if pcm.size == 0:
                return False
            rms = float(np.sqrt(np.mean(np.square(pcm.astype(np.float32)))))
            effective_threshold = max(
                self.speech_rms_threshold, self.noise_floor_rms + self.speech_rms_margin
            )
            loud_enough = rms >= effective_threshold
            is_speech = loud_enough and self._is_voice_like(
                pcm, self.audio_handler.config.SEND_SAMPLE_RATE
            )
            if not loud_enough:
                # Track the floor from quiet chunks only. Letting loud non-voice noise
                # raise it would desensitise the mic to the next real utterance.
                self.noise_floor_rms += (rms - self.noise_floor_rms) * self.noise_floor_alpha
            return is_speech
        except Exception:
            return False

    def _drain_microphone_backlog(self) -> int:
        """Discard mic audio captured while the assistant was talking.

        PyAudio keeps filling its input ring buffer for the whole reply. The is_speaking
        flag only masks chunks as they are READ - one chunk per loop iteration - so after a
        long reply the backlog still holds the assistant's own voice and would confirm a
        bogus utterance. Only ever called from process_audio_input, the single reader of
        this stream: two threads must never read the same PyAudio stream concurrently.
        """
        stream = self.audio_handler.audio_stream
        if stream is None:
            return 0
        chunk_size = self.audio_handler.config.CHUNK_SIZE
        dropped = 0
        try:
            while stream.get_read_available() >= chunk_size and dropped < 400:
                stream.read(chunk_size, exception_on_overflow=False)
                dropped += 1
        except Exception as e:
            logger.warning("Draining the mic backlog failed: %s", e)
        return dropped

    @staticmethod
    def _voice_language(voice) -> str:
        """Language tag for a SAPI voice, e.g. 'en-gb', parsed from its registry id."""
        match = re.search(r"_([A-Za-z]{2}-[A-Za-z]{2})_", str(getattr(voice, "id", "")))
        if match:
            return match.group(1).lower()
        return ""

    @staticmethod
    def _contains_cjk(text: str) -> bool:
        return bool(re.search(r"[぀-ゟ゠-ヿ一-鿿]", text))

    def _detect_tts_voices(self) -> None:
        """Record which languages this machine can actually SPEAK.

        Windows ships only the voices for installed language packs. Handing Japanese
        text to an English-only voice does not fail - SAPI returns success and plays
        nothing at all, so the assistant looks broken rather than misconfigured.
        """
        pythoncom.CoInitialize()
        engine = None
        try:
            engine = pyttsx3.init()
            for voice in engine.getProperty("voices"):
                language = self._voice_language(voice)
                if language:
                    self.tts_languages.add(language)
                    self.tts_voice_by_language.setdefault(language, voice.id)
        except Exception as e:
            logger.warning("Could not enumerate speech voices: %s", e)
        finally:
            if engine is not None:
                try:
                    engine.stop()
                except Exception:
                    pass
            pythoncom.CoUninitialize()

    def _voice_id_for_text(self, text: str) -> Optional[str]:
        """Pick an installed voice that can pronounce this text."""
        if self._contains_cjk(text):
            for language, voice_id in self.tts_voice_by_language.items():
                if language.startswith(("ja", "zh", "ko")):
                    return voice_id
            return None
        if self.tts_voice_hint:
            for language, voice_id in self.tts_voice_by_language.items():
                if self.tts_voice_hint in language:
                    return voice_id
        return None

    def _speak_sync(self, text: str) -> None:
        """Speak on one COM-initialized worker thread (required by Windows SAPI)."""
        pythoncom.CoInitialize()
        engine = None
        try:
            engine = pyttsx3.init()
            engine.setProperty("rate", self.tts_rate)

            voice_id = self._voice_id_for_text(text)
            if voice_id:
                engine.setProperty("voice", voice_id)
            elif self.tts_voice_hint:
                for voice in engine.getProperty("voices"):
                    if self.tts_voice_hint in str(voice.name).lower():
                        engine.setProperty("voice", voice.id)
                        break

            engine.say(text)
            engine.runAndWait()
        finally:
            if engine is not None:
                engine.stop()
            pythoncom.CoUninitialize()

    def _pcm_to_wav(self, pcm_audio: bytes) -> bytes:
        """Wrap captured 16-bit PCM in a standard WAV container for Gemini."""
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(self.audio_handler.config.CHANNELS)
            wav_file.setsampwidth(2)  # paInt16
            wav_file.setframerate(self.audio_handler.config.SEND_SAMPLE_RATE)
            wav_file.writeframes(pcm_audio)
        return wav_buffer.getvalue()

    async def speak_text(self, text: str) -> None:
        cleaned = (text or "").strip()
        if not cleaned:
            return
        if cleaned.endswith(("?", "？")):
            self.last_question_at = time.monotonic()
        try:
            async with self.tts_lock:
                logger.info("Speaking AI response through the Windows default output device.")
                self.is_speaking = True
                try:
                    await asyncio.to_thread(self._speak_sync, cleaned)
                finally:
                    try:
                        # SAPI returns when the last sample reaches the device; the room
                        # still carries the tail for a moment.
                        await asyncio.sleep(self.tts_tail_guard_sec)
                    finally:
                        # Must survive cancellation, or the mic gate latches shut forever.
                        self._mic_drain_pending = True
                        self.is_speaking = False
        except Exception as e:
            logger.error(f"音声読み上げ中にエラーが発生しました: {e}")

    # -------------------------------------------------------------------- api

    @property
    def api_temporarily_blocked(self) -> bool:
        return time.monotonic() < self.api_blocked_until

    def _handle_api_error(self, error: Exception, context: str) -> None:
        """Classify API errors and back off for a bounded window on quota exhaustion."""
        error_text = str(error)
        status = getattr(error, "code", None) or getattr(error, "status_code", None)
        quota_hit = (
            status == 429
            or "RESOURCE_EXHAUSTED" in error_text
            or "prepayment credits are depleted" in error_text
            or "quota" in error_text.lower()
        )
        if quota_hit:
            if not self.api_temporarily_blocked:
                logger.error(
                    "Gemini API quota/credit limit hit. Pausing API calls for %ss.",
                    self.api_backoff_sec,
                )
            self.api_blocked_until = time.monotonic() + self.api_backoff_sec
            return

        logger.error(f"{context}: {error}")

    @staticmethod
    def _parse_json_response(raw: Optional[str]) -> dict:
        """Parse model JSON defensively.

        Even in JSON mode the text can come back wrapped in markdown fences or empty.
        A hard failure here used to drop the whole turn in silence.
        """
        text = (raw or "").strip()
        if not text:
            return {}
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, flags=re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group(0))
                    return parsed if isinstance(parsed, dict) else {}
                except json.JSONDecodeError:
                    pass
            logger.warning("Model returned unparseable JSON: %s", text[:200])
            return {}

    def _webcam_rules(self) -> str:
        return (
            "WEBCAM: a live frame from the webcam facing the user is attached.\n"
            "- Actually look at it. Notice the person and the room: posture, expression, "
            "lighting, time-of-day feel, what is on the desk, whether they look tired, "
            "focused, or relaxed.\n"
            "- Weave ONE natural observation or question about what you see into your reply "
            "when it fits - for example noticing the light has changed, that they have been "
            "sitting a long time, or asking how they are doing after a long session. Do NOT "
            "do this every single turn; roughly one turn in three feels natural.\n"
            "- Describe only what is plainly visible. Never guess identity, age, health, "
            "ethnicity, or mood as if it were fact - phrase impressions softly (\"you look \" "
            "\"like you might be...\", \"is it getting late there?\").\n"
            "- Never comment on appearance in a way that could feel judgemental or personal "
            "about looks. Comment on the situation and energy, not attractiveness.\n"
            "- If the frame is dark, empty, or unclear, silently ignore it and say nothing "
            "about it.\n"
            "- visual_note is only the passing moment. If you notice something DURABLE "
            "about how they look or about the room itself - they wear round glasses, the "
            "room has three other desks, there is a whiteboard behind them - put that in "
            "memory once, under 'appearance' or 'room'.\n"
        )

    def _screen_rules(self) -> str:
        """When to look at the monitor instead of the webcam.

        Written as a decision rule rather than a keyword list: the assistant used to
        answer "what's wrong?" with generic advice, and once replied "just ask me to
        check your screen" instead of simply checking it.
        """
        return (
            "SCREEN OR WEBCAM - decide deliberately every turn:\n"
            "- The webcam shows the PERSON and the ROOM. It does NOT show their monitor. A "
            "screenshot shows their COMPUTER SCREEN.\n"
            "- Set needs_screen TRUE whenever the answer depends on what is on their computer: "
            "their code, an error, a warning, a terminal, a document, a paper, a web page, a "
            "chart, a setting - or vague references to it like \"this\", \"here\", \"that one\", "
            "\"what am I working on\", \"what's wrong\", \"what do you think\", \"help me with "
            "this\", or any request to read, check, look at, or fix something.\n"
            "- If they say anything like \"screen\", \"my screen\", \"look\", \"check this\" - in "
            "ANY language, including Japanese - that is a request to look at the screen. Set "
            "needs_screen TRUE.\n"
            "- NEVER explain that you are ABLE to look at their screen, and never ask them to "
            "ask again. If looking would help, just set needs_screen true and look. Describing "
            "the capability instead of using it is a failure.\n"
            "- When a question could be about either the room or the computer and it concerns "
            "their WORK, prefer the screen.\n"
            "- Use the webcam view only for questions about them, the room, or physical objects "
            "around them - their posture, energy, a note or book on the desk, the light.\n"
            "- If you are genuinely unsure and the question is not about them personally, "
            "looking at the screen is the safer choice.\n"
        )

    def _turn_json_contract(self, with_transcript: bool, with_screen_flag: bool = False) -> str:
        fields = (
            '"transcript": "the exact words the user spoke, verbatim", '
            '"addressed_to_you": true only if they are actually speaking TO you and want an '
            "answer; false if they are muttering, thinking aloud, reading aloud, talking to "
            "someone else, or it is just a filler sound or trailing fragment, "
            if with_transcript
            else ""
        )
        if with_screen_flag and self.screen_capture_enabled:
            fields += (
                '"needs_screen": true or false, decided by the SCREEN OR WEBCAM rules above. '
                "When true, leave reply as an empty string - a screenshot will be captured and "
                "you will be asked again with it attached. "
            )
        return (
            "Return JSON only, with these fields: {"
            + fields
            + '"reply": "what you say out loud", '
            '"visual_note": "one short factual sentence describing what you can see in the '
            'webcam frame right now, or an empty string if you cannot see anything useful. '
            'Refer to the person as \\"they\\" or \\"the user\\" here - never write a name in '
            'this field", '
            '"memory": [{"category": "identity|personal|research|hobbies|life|appearance|room",'
            ' "content": "one short third-person sentence"}]'
            "}. "
            "The memory list is how you get to know this person over months. Add an entry "
            "ONLY for a NEW, lasting thing about WHO THEY ARE that the profile above does "
            "not already say. The categories are:\n"
            "- identity: their name, and how they like to be addressed.\n"
            "- personal: general things about them - what they study, where they work, what "
            "stage they are at, which languages they speak, how they like to work.\n"
            "- research: their research INTERESTS in general terms only - the field, or the "
            "kind of question that fascinates them. Never a specific paper or project.\n"
            "- hobbies: what they enjoy - sport, music, games, reading, cooking, travel.\n"
            "- life: life outside work - where they are from, family, friends, food, "
            "commute, pets, how they relax.\n"
            "- appearance: durable things about how they look - glasses, hair, usual style. "
            "Neutral and factual only, never a judgement about looks.\n"
            "- room: what this room is like - that it is a shared PhD room, the desk, the "
            "window, the whiteboard, the plants, the air conditioner.\n"
            "NEVER store any of the following, under any category. None of it is who the "
            "person is, all of it is stale within a day, and storing it makes you nag:\n"
            "- dates, times, days of the week, months, or \"today\"/\"tomorrow\"\n"
            "- deadlines, submission dates, or conference dates\n"
            "- meetings, seminars, appointments, or anything from their calendar\n"
            "- schedules, plans, to-do lists, tasks, or what they intend to do next\n"
            "- anything specific to one paper: its topic, title, outline, table of "
            "contents, chapters, sections, drafts, or the literature they are reading\n"
            "- things they asked YOU to do, your own suggestions, small talk, or questions\n"
            "Return an empty list on most turns - zero new facts is normal and correct, and "
            "one good one a day is plenty. "
            "visual_note is for your own records, not spoken aloud."
        )

    async def _generate_voice_turn(
        self, pcm_audio: bytes, camera_frame: Optional[Image.Image]
    ) -> Optional[dict]:
        """Transcript + reply + what it sees + new memories, in ONE model request."""
        if self.api_temporarily_blocked:
            return None

        audio_part = types.Part.from_bytes(
            data=self._pcm_to_wav(pcm_audio),
            mime_type="audio/wav",
        )
        media_parts: list[object] = [audio_part]
        if camera_frame is not None:
            media_parts.append(camera_frame)

        instruction = (
            "The attached audio is the user speaking to you. Transcribe it faithfully, then "
            "answer what they actually asked - directly, not with a generic greeting.\n\n"
            + (self._webcam_rules() if camera_frame is not None else "")
            + "\n"
            + (self._screen_rules() if self.screen_capture_enabled else "")
            + "\n"
            + self._turn_json_contract(with_transcript=True, with_screen_flag=True)
            + "\n\nSILENCE IS THE RIGHT ANSWER TO NOISE. If the audio holds no intelligible "
            "human speech addressed to you - silence, breathing, typing, a door, a chair, "
            "music, traffic, a TV, or distant chatter you cannot make out - set BOTH transcript "
            "and reply to empty strings. Do not greet, do not invent an opener, do not ask them "
            "to repeat, do not comment on the noise.\n"
            "NEVER GUESS WORDS. Transcribing noise as a common phrase like \"Hello\", \"Thank "
            "you\", \"Yes\" or \"ありがとう\" when you did not clearly hear it is a serious "
            "error - return an empty transcript instead.\n"
            "ONLY ANSWER WHEN SPOKEN TO. People think out loud while they work. If they mutter, "
            "trail off, read something aloud, or make a hesitation sound like \"ah\", \"ummm\", "
            "\"hmm\", \"uh\", \"じゃあ\", \"えーと\", \"あの\", \"well\", \"so\" with nothing else, "
            "set addressed_to_you to false and "
            "reply to an empty string. Do not answer a fragment that was not aimed at you, and "
            "do not treat it as an invitation to continue the previous topic. Staying quiet "
            "while they think is what a good room-mate does.\n\n"
            + self._variation_directives()
        )

        try:
            response = await self.client.aio.models.generate_content(
                model=config.gemini.MODEL,
                contents=[self._compose_prompt(instruction), *media_parts],
                config=types.GenerateContentConfig(responseMimeType="application/json"),
            )
            return self._parse_json_response(getattr(response, "text", None))
        except Exception as e:
            self._handle_api_error(e, "Voice request failed")
            return None

    async def _generate_text_turn(self, text: str) -> Optional[dict]:
        """Typed-message turn, same JSON contract, no audio."""
        if self.api_temporarily_blocked:
            return None
        instruction = (
            f"The person typed: {text}\nRespond helpfully.\n\n"
            + (self._screen_rules() if self.screen_capture_enabled else "")
            + "\n"
            + self._turn_json_contract(with_transcript=False, with_screen_flag=True)
            + "\n\n"
            + self._variation_directives()
        )
        try:
            response = await self.client.aio.models.generate_content(
                model=config.gemini.MODEL,
                contents=self._compose_prompt(instruction),
                config=types.GenerateContentConfig(responseMimeType="application/json"),
            )
            return self._parse_json_response(getattr(response, "text", None))
        except Exception as e:
            self._handle_api_error(e, "Text request failed")
            return None

    async def _capture_screen_image(self) -> Optional[Image.Image]:
        """Screenshot the desktop, off the event loop."""
        if not self.screen_capture_enabled:
            return None
        try:
            image = await asyncio.to_thread(
                grab_screen_image, self.screen_monitor, self.screen_max_edge
            )
            if image is not None:
                logger.info("Screenshot captured for a screen-related question.")
            return image
        except Exception as e:
            logger.warning("Screen capture failed: %s", e)
            return None

    async def _answer_with_screen(
        self, question: str, screen_image: Image.Image, camera_frame: Optional[Image.Image]
    ) -> Optional[dict]:
        """Second pass: answer a screen question with the screenshot attached."""
        if self.api_temporarily_blocked:
            return None
        media_parts: list[object] = [screen_image]
        if camera_frame is not None:
            media_parts.append(camera_frame)

        instruction = (
            f"The user asked: {question}\n\n"
            "SCREENSHOT: the FIRST image attached is a screenshot of their computer screen, "
            "taken just now. Read it carefully and answer their question about it - quote exact "
            "error text, file names, or values when that is what they asked for. Describe what "
            "is actually on screen; never guess at content you cannot read. If the screenshot is "
            "unreadable or does not contain what they asked about, say so plainly.\n"
            + (
                "The SECOND image is the webcam view of the room - use it only for context "
                "about the person, not for the screen question.\n"
                if camera_frame is not None
                else ""
            )
            + "\nYou are speaking out loud, so keep it concise and easy to follow. Lead with the "
            "answer.\n\n"
            + self._turn_json_contract(with_transcript=False)
            + "\n\n"
            + self._variation_directives()
        )
        try:
            response = await self.client.aio.models.generate_content(
                model=config.gemini.MODEL,
                contents=[self._compose_prompt(instruction), *media_parts],
                config=types.GenerateContentConfig(responseMimeType="application/json"),
            )
            return self._parse_json_response(getattr(response, "text", None))
        except Exception as e:
            self._handle_api_error(e, "Screen-aware request failed")
            return None

    async def _resolve_screen_request(
        self, result: dict, question: str, camera_frame: Optional[Image.Image]
    ) -> dict:
        """If the model asked to see the screen, capture it and answer again.

        Costs a second API call, but only for turns that genuinely need the screen -
        attaching a screenshot to every turn would add image tokens and latency to all
        the ones that do not.
        """
        if not result.get("needs_screen") or not self.screen_capture_enabled:
            return result
        if not question.strip():
            return result

        screen_image = await self._capture_screen_image()
        if screen_image is None:
            result["reply"] = (
                "I tried to look at your screen but couldn't capture it just now."
            )
            return result

        second = await self._answer_with_screen(question, screen_image, camera_frame)
        if second and str(second.get("reply", "") or "").strip():
            return second
        # Fall back rather than going silent if the second pass failed.
        if not str(result.get("reply", "") or "").strip():
            result["reply"] = "I couldn't read your screen just now - could you try again?"
        return result

    def _absorb_turn_result(self, result: dict, kind: str, exchange: str) -> None:
        """Record everything the model chose to remember from one turn."""
        visual_note = str(result.get("visual_note", "") or "").strip()
        if visual_note:
            self._remember_visual_note(visual_note)

        facts = result.get("memory")
        if isinstance(facts, list):
            # dict is REQUIRED here: the contract now returns {"category", "content"}
            # objects, and filtering them out would silently discard every new fact.
            self._remember_facts(
                [f for f in facts if isinstance(f, (str, int, float, dict))]
            )
        elif isinstance(facts, str) and facts.strip():
            self._remember_facts([facts])

        self._remember_observation(kind, exchange)
        self._refresh_dynamic_system_prompt()
        self._save_memory()

    # -------------------------------------------------------------- proactive

    def _proactive_conditions_ok(self) -> bool:
        """Every guard EXCEPT the proactive lock itself.

        Deliberately excludes proactive_lock: the greeting coroutine re-checks these
        conditions *after* acquiring that lock, so testing it here would make the
        coroutine see its own lock held and bail out every time.
        """
        if self.api_temporarily_blocked or not self.is_running:
            return False
        if self.is_speaking or self.tts_lock.locked():
            return False
        if self.pending_turns > 0:
            return False
        if self.audio_out_queue is not None and not self.audio_out_queue.empty():
            return False
        now = time.monotonic()
        return (
            (now - self.last_proactive_at) >= self.proactive_cooldown_sec
            and (now - self.last_interaction_at) >= self.proactive_min_idle_sec
        )

    def _proactive_gate_open(self) -> bool:
        """True when opening a conversation would be welcome rather than nagging."""
        return not self.proactive_lock.locked() and self._proactive_conditions_ok()

    def _log_background_task_result(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("Proactive greeting task failed: %s", error)

    def _maybe_start_proactive_turn(self, camera_frame: Optional[Image.Image]) -> None:
        """Spawn the greeting off the mic loop so audio capture never stalls on the API."""
        if not self._proactive_gate_open():
            return
        if self._proactive_task is not None and not self._proactive_task.done():
            return
        self._proactive_task = asyncio.get_running_loop().create_task(
            self.proactive_greet_on_speech(camera_frame)
        )
        self._proactive_task.add_done_callback(self._log_background_task_result)

    async def proactive_greet_on_speech(self, camera_frame: Optional[Image.Image] = None) -> None:
        """Speak first, because a human voice was heard nearby but not addressed to us."""
        async with self.proactive_lock:
            # Re-check under the lock, minus the lock itself (we hold it).
            if not self._proactive_conditions_ok():
                return
            self.last_proactive_at = time.monotonic()

            media_parts: list[object] = [camera_frame] if camera_frame is not None else []
            instruction = (
                "You just heard someone moving or speaking nearby, but they did not address "
                "you directly. Open the conversation yourself: one short, warm, natural line "
                "- the kind of thing a person sharing the room would say. Reference what you "
                "can see or something you remember about them if it fits. Do not announce "
                "that you detected a sound, and do not launch into a checklist.\n\n"
                + (self._webcam_rules() if camera_frame is not None else "")
                + "\n"
                + self._turn_json_contract(with_transcript=False)
                + "\n\nKeep reply under 35 words.\n\n"
                + self._variation_directives()
            )
            try:
                response = await self.client.aio.models.generate_content(
                    model=config.gemini.MODEL,
                    contents=[self._compose_prompt(instruction), *media_parts],
                    config=types.GenerateContentConfig(responseMimeType="application/json"),
                )
                result = self._parse_json_response(getattr(response, "text", None))
            except Exception as e:
                self._handle_api_error(e, "Proactive greeting failed")
                return

            reply = str(result.get("reply", "") or "").strip()
            if not reply:
                return
            print("\nAI (proactive): " + to_markdown(reply))
            await self.speak_text(reply)
            self.last_interaction_at = time.monotonic()
            self._absorb_turn_result(result, "proactive", f"Assistant opened with: {reply}")

    async def _safe_startup_greeting(self) -> None:
        """A failed opener must never take down the TaskGroup (and with it the app)."""
        try:
            await self.startup_greeting()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Startup greeting failed: %s", e)

    async def startup_greeting(self) -> None:
        """Session opener: photograph the room, say what is seen, greet, and ask something.

        Bypasses the proactive cooldown/idle gates on purpose (it is the one guaranteed
        opener per session), but still takes proactive_lock so it can't overlap a greeting.
        """
        async with self.proactive_lock:
            if not self.is_running or self.api_temporarily_blocked:
                return
            camera_frame = await self._capture_camera_frame()
            media_parts: list[object] = [camera_frame] if camera_frame is not None else []
            instruction = (
                "The session has just started and this is your FIRST line. Look at the "
                "attached webcam photo and, in 2-3 short spoken sentences: (1) say what you "
                "see - the PhD room / desk situation and the person if present (what they "
                "look like they are doing, mood, anything notable); (2) greet them warmly; "
                "(3) end with ONE natural question grounded in what you see or remember "
                "about them. If nobody is visible, describe the room, greet anyway, and ask "
                "when they will be around. If no photo is attached, say you cannot see "
                "anything yet, greet, and ask a question. Do not invent details you cannot "
                "see. No checklists, no deadlines or calendar talk.\n\n"
                + (self._webcam_rules() if camera_frame is not None else "")
                + "\n"
                + self._turn_json_contract(with_transcript=False)
                + "\n\nKeep reply under 60 words."
            )
            try:
                response = await self.client.aio.models.generate_content(
                    model=config.gemini.MODEL,
                    contents=[self._compose_prompt(instruction), *media_parts],
                    config=types.GenerateContentConfig(responseMimeType="application/json"),
                )
                result = self._parse_json_response(getattr(response, "text", None))
            except Exception as e:
                self._handle_api_error(e, "Startup greeting failed")
                return

            reply = str(result.get("reply", "") or "").strip()
            if not reply:
                return
            self.last_proactive_at = time.monotonic()
            print("\nAI (startup): " + to_markdown(reply))
            await self.speak_text(reply)
            self.last_interaction_at = time.monotonic()
            self._absorb_turn_result(result, "startup", f"Assistant opened with: {reply}")

    # ------------------------------------------------------------------ loops

    async def send_text(self) -> None:
        """テキスト入力を処理し、AIに送信"""
        while self.is_running:
            try:
                text = await asyncio.to_thread(input, "message > ")
            except EOFError:
                # No interactive console (launched detached / piped). Voice still works,
                # so stop reading stdin instead of spinning on EOF forever.
                logger.info("No interactive stdin; running in voice-only mode.")
                return
            except Exception as e:
                logger.error(f"テキスト入力の読み取りに失敗しました: {e}")
                return

            try:
                if text.strip().lower() == "q":
                    if self.stop_event is not None:
                        self.stop_event.set()
                    return
                if not text.strip():
                    continue
                if self.api_temporarily_blocked:
                    print("\nAI: API access is paused briefly (usage limit). Try again shortly.")
                    continue

                self.pending_turns += 1
                try:
                    result = await self._generate_text_turn(text)
                    if result and result.get("needs_screen"):
                        result = await self._resolve_screen_request(result, text, None)
                finally:
                    self.pending_turns -= 1

                self.last_interaction_at = time.monotonic()
                reply = str((result or {}).get("reply", "") or "").strip()
                if reply:
                    print("\nAI: " + to_markdown(reply))
                    await self.speak_text(reply)
                    self.last_interaction_at = time.monotonic()
                    self._absorb_turn_result(
                        result, "dialog", f"User: {text}\nAssistant: {reply}"
                    )
                else:
                    print("\nAI: Sorry, I could not process that just now.")
            except Exception as e:
                logger.error(f"テキスト送信中にエラーが発生しました: {e}")
                await asyncio.sleep(1)

    async def _capture_camera_frame(self) -> Optional[Image.Image]:
        """Take one webcam frame at the instant detected speech begins."""
        if self.camera_capture is None:
            return None
        try:
            image = await asyncio.to_thread(self.camera_capture.capture_frame)
            if image is not None:
                logger.info("Camera frame captured for detected speech.")
            return image
        except Exception as e:
            logger.warning("Camera capture for detected speech failed: %s", e)
            return None

    async def process_audio_input(self) -> None:
        """Buffer detected speech from the microphone for transcription."""
        speech_chunks: list[bytes] = []
        pending_chunks: list[bytes] = []
        pending_speech_count = 0
        voiced_chunks = 0
        camera_frame: Optional[Image.Image] = None
        silent_chunks = 0
        max_chunks = max(
            1,
            int(self.voice_max_seconds * self.audio_handler.config.SEND_SAMPLE_RATE /
                self.audio_handler.config.CHUNK_SIZE),
        )
        while self.is_running:
            try:
                audio_data = await self.audio_handler.read_audio()
                if not audio_data:
                    # read_audio() swallows stream errors and returns None; without this
                    # pause a dead mic device would spin the loop at 100% CPU.
                    await asyncio.sleep(0.1)
                    continue

                if self.is_speaking:
                    # Do not mistake the assistant's own speaker output for a new utterance.
                    speech_chunks = []
                    pending_chunks = []
                    pending_speech_count = 0
                    voiced_chunks = 0
                    silent_chunks = 0
                    camera_frame = None
                    continue

                if self._mic_drain_pending:
                    self._mic_drain_pending = False
                    dropped = await asyncio.to_thread(self._drain_microphone_backlog)
                    if dropped:
                        logger.info(
                            "Dropped %s buffered chunks of the assistant's own speech.", dropped
                        )
                    continue

                chunk = audio_data.get("data", b"")
                is_speech = self._is_speech_chunk(audio_data)

                if speech_chunks:
                    # Already inside a confirmed utterance; preserve a short tail so final
                    # words are not clipped.
                    speech_chunks.append(chunk)
                    if is_speech:
                        voiced_chunks += 1
                        silent_chunks = 0
                    else:
                        silent_chunks += 1
                elif is_speech:
                    # Require a short run of speech-level chunks before committing to an
                    # utterance, so a single noise spike can't fire the camera/reply pipeline.
                    pending_chunks.append(chunk)
                    pending_speech_count += 1
                    if pending_speech_count >= self.voice_attack_chunks:
                        logger.info("Speech detected on the configured microphone.")
                        camera_frame = await self._capture_camera_frame()
                        speech_chunks = pending_chunks
                        voiced_chunks = pending_speech_count
                        pending_chunks = []
                        pending_speech_count = 0
                else:
                    pending_chunks = []
                    pending_speech_count = 0

                if speech_chunks and (
                    silent_chunks >= self.voice_silence_chunks or len(speech_chunks) >= max_chunks
                ):
                    utterance = b"".join(speech_chunks)
                    speech_like = self._utterance_is_speech_like(
                        np.frombuffer(utterance, dtype=np.int16),
                        self.audio_handler.config.SEND_SAMPLE_RATE,
                    )

                    # Count only VOICED chunks: the silence tail is always appended, so
                    # measuring total length would let every cough through as a "sentence"
                    # and cost a full audio+image API call.
                    if not speech_like:
                        flux, cov = self._utterance_modulation(
                            np.frombuffer(utterance, dtype=np.int16),
                            self.audio_handler.config.SEND_SAMPLE_RATE,
                        )
                        logger.info(
                            "Ignored a non-speech sound (flux=%.2f level_var=%.2f); "
                            "nothing sent.",
                            flux,
                            cov,
                        )
                    elif voiced_chunks >= self.voice_min_voiced_chunks:
                        try:
                            self.audio_out_queue.put_nowait(
                                (b"".join(speech_chunks), camera_frame)
                            )
                        except asyncio.QueueFull:
                            logger.warning("Voice queue full; dropping the oldest utterance.")
                            try:
                                self.audio_out_queue.get_nowait()
                                self.audio_out_queue.put_nowait(
                                    (b"".join(speech_chunks), camera_frame)
                                )
                            except (asyncio.QueueEmpty, asyncio.QueueFull):
                                pass
                    else:
                        # A human was audible but did not address us. This is the moment to
                        # open a conversation - and the branch that previously dropped the
                        # audio in silence.
                        logger.info(
                            "Heard a voice but not a full utterance (%s voiced chunks).",
                            voiced_chunks,
                        )
                        self._maybe_start_proactive_turn(camera_frame)

                    speech_chunks = []
                    voiced_chunks = 0
                    silent_chunks = 0
                    camera_frame = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"音声入力処理中にエラーが発生しました: {e}")
                await asyncio.sleep(1)

    async def send_realtime_data(self) -> None:
        """Send each captured voice utterance and its camera context to Gemini."""
        while self.is_running:
            try:
                item = await self.audio_out_queue.get()
                if item is None or not self.is_running:
                    return
                pcm_audio, camera_frame = item

                if self.api_temporarily_blocked:
                    if not self._quota_notice_spoken:
                        self._quota_notice_spoken = True
                        await self.speak_text(
                            "I've hit my usage limit for a moment. I'll be back shortly."
                        )
                    continue
                self._quota_notice_spoken = False

                logger.info("Sending detected speech to Gemini for a direct response.")
                self.pending_turns += 1
                try:
                    result = await self._generate_voice_turn(pcm_audio, camera_frame)
                finally:
                    self.pending_turns -= 1

                self.last_interaction_at = time.monotonic()
                if not result:
                    continue

                transcript = str(result.get("transcript", "") or "").strip()
                just_asked = (time.monotonic() - self.last_question_at) <= 45
                if not self._is_meaningful_transcript(transcript, short_answer_ok=just_asked):
                    # It was noise or filler, not a message. Say nothing at all -
                    # answering noise is worse than missing a word.
                    logger.info(
                        "Ignoring non-speech audio (transcript=%r).", transcript[:60]
                    )
                    continue
                if result.get("addressed_to_you") is False:
                    # Real words, but they were thinking aloud rather than asking us.
                    logger.info("Not addressed to me, staying quiet: %r", transcript[:60])
                    continue

                if result.get("needs_screen"):
                    self.pending_turns += 1
                    try:
                        result = await self._resolve_screen_request(
                            result, transcript, camera_frame
                        )
                    finally:
                        self.pending_turns -= 1

                reply = str(result.get("reply", "") or "").strip()
                print("\nYou (voice): " + transcript)
                if reply:
                    print("\nAI (voice): " + to_markdown(reply))
                    await self.speak_text(reply)
                    self.last_interaction_at = time.monotonic()
                    self._absorb_turn_result(
                        result, "voice_dialog", f"User: {transcript}\nAssistant: {reply}"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Voice/camera queue processing failed: {e}")
                await asyncio.sleep(1)

    async def run(self) -> None:
        """アプリケーションのメインループを実行"""
        try:
            if sys.platform != 'win32':
                raise RuntimeError("このバージョンはWindows専用です。")

            self.audio_out_queue = asyncio.Queue(maxsize=5)
            self.stop_event = asyncio.Event()

            await self.audio_handler.setup_input_stream()
            await self.audio_handler.setup_output_stream()
            self.camera_capture = CameraCaptureWindows()
            await asyncio.to_thread(self.camera_capture.open)

            await asyncio.to_thread(self._detect_tts_voices)
            speaks_cjk = any(
                language.startswith(("ja", "zh", "ko")) for language in self.tts_languages
            )
            logger.info(
                "Speech voices available for: %s",
                ", ".join(sorted(self.tts_languages)) or "none detected",
            )
            if not speaks_cjk:
                # Replying in Japanese with only English voices installed plays SILENCE.
                self.reply_language_rule = (
                    "- ALWAYS reply in English, even when the user speaks Japanese or another "
                    "language. The speech engine on this machine can only pronounce English, so "
                    "a reply in any other language is heard as total silence. You still "
                    "UNDERSTAND every language - transcribe what they said in their own "
                    "language, but write your reply in clear, simple English.\n"
                )
                logger.warning(
                    "No Japanese/Chinese/Korean speech voice is installed - forcing English "
                    "replies so they are audible. To speak Japanese, install it under "
                    "Settings > Time & language > Language & region > add Japanese, "
                    "including its Speech feature, then restart."
                )
            self._refresh_dynamic_system_prompt()

            self._enable_keep_awake_mode()
            logger.info(
                "音声検出しきい値を設定しました: SPEECH_RMS_THRESHOLD=%s margin=%s",
                self.speech_rms_threshold,
                self.speech_rms_margin,
            )
            logger.info("アプリケーションを開始します")

            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.send_text())
                realtime_task = tg.create_task(self.send_realtime_data())
                mic_task = tg.create_task(self.process_audio_input())
                tg.create_task(self._safe_startup_greeting())

                await self.stop_event.wait()
                self.is_running = False

                # Never `await queue.put(None)` here: audio_out_queue is bounded and
                # process_audio_input is a competing putter, so a full queue would
                # deadlock run() inside the async-with, before __aexit__ ever runs.
                try:
                    self.audio_out_queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
                # Backstop for anything parked in an await. TaskGroup._on_task_done()
                # returns early for cancelled tasks, so this raises no ExceptionGroup.
                realtime_task.cancel()
                mic_task.cancel()

        except asyncio.CancelledError:
            logger.info("アプリケーションがキャンセルされました")
            self.is_running = False
        except BaseExceptionGroup as eg:
            logger.error("エラーが発生しました:")
            traceback.print_exception(eg)
        except Exception as e:
            logger.error(f"起動中にエラーが発生しました: {e}")
            traceback.print_exc()
        finally:
            self.is_running = False
            self._disable_keep_awake_mode()
            if self._proactive_task is not None and not self._proactive_task.done():
                self._proactive_task.cancel()
            payload, self._pending_memory_payload = self._pending_memory_payload, None
            if payload is not None:
                self._write_memory_payload(payload)
            if self.camera_capture is not None:
                self.camera_capture.close()
            self.audio_handler.cleanup()
            logger.info("アプリケーションを終了します")

def main():
    """アプリケーションのエントリーポイント"""
    if sys.platform != 'win32':
        print("このバージョンはWindows専用です。")
        sys.exit(1)
    app = TalkScreenAI()
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        logger.info("Ctrl+C を受け取りました。終了します。")

if __name__ == "__main__":
    main()
