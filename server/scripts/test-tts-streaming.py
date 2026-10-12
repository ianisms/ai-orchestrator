import os
import random
from pathlib import Path
import subprocess
from urllib.parse import urlencode
from urllib.request import urlopen


_WORDS_PATH = Path("/ai/server/scripts/wordlist.txt")


def _load_words(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Wordlist not found: {path}")
    words = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        words.append(entry)
    if not words:
        raise ValueError(f"Wordlist is empty: {path}")
    return words


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _strip_trailing_punct(word: str) -> str:
    return word.rstrip(".,;:!?")


def _punctuate_words(words: list[str]) -> list[str]:
    if len(words) < 2:
        return words

    out = []
    for idx, word in enumerate(words):
        if idx < len(words) - 1:
            roll = random.random()
            if roll < 0.12:
                word = f"{word},"
            elif roll < 0.14:
                word = f"{word};"
            elif roll < 0.16:
                word = f"{word}:"
            elif roll < 0.18:
                word = f"{word}..."
        out.append(word)
    return out


def _ensure_inline_punct(sentences: list[list[str]], marks: list[str]) -> None:
    if not sentences:
        return

    present = {mark: False for mark in marks}
    for words in sentences:
        for word in words:
            for mark in marks:
                if word.endswith(mark):
                    present[mark] = True

    for mark, ok in present.items():
        if ok:
            continue
        candidates = [i for i, words in enumerate(sentences) if len(words) >= 2]
        if not candidates:
            candidates = list(range(len(sentences)))
        if not candidates:
            return
        sent_idx = random.choice(candidates)
        words = sentences[sent_idx]
        idx_max = max(0, len(words) - 2)
        word_idx = random.randint(0, idx_max)
        base = _strip_trailing_punct(words[word_idx])
        words[word_idx] = f"{base}{mark}"


def _sentence_ending() -> str:
    roll = random.random()
    if roll < 0.12:
        return "!"
    if roll < 0.22:
        return "?"
    return "."


def random_paragraph(
    min_sentences: int = 3,
    max_sentences: int = 6,
    min_words: int = 8,
    max_words: int = 16,
    seed: int = None,
) -> str:
    if seed is not None:
        random.seed(seed)

    sentence_count = random.randint(min_sentences, max_sentences)
    sentences_words: list[list[str]] = []
    endings: list[str] = []

    words_pool = _load_words(_WORDS_PATH)
    for _ in range(sentence_count):
        word_count = random.randint(min_words, max_words)
        words = [random.choice(words_pool) for _ in range(word_count)]
        words = _punctuate_words(words)
        sentences_words.append(words)
        endings.append(_sentence_ending())

    ending_set = set(endings)
    for mark in [".", "?", "!"]:
        if mark not in ending_set:
            extra_len = random.randint(3, 5)
            extra_words = [random.choice(words_pool) for _ in range(extra_len)]
            sentences_words.append(_punctuate_words(extra_words))
            endings.append(mark)
            ending_set.add(mark)

    _ensure_inline_punct(sentences_words, [",", ";", ":", "..."])

    sentences = []
    for words, ending in zip(sentences_words, endings):
        sentence = " ".join(words).capitalize() + ending
        sentences.append(sentence)
    return " ".join(sentences)


def main():
    subprocess.run(["python", "kill_zombiez.py"], check=False)

    audio_path = Path("/ai/server/scripts/test-streaming.wav")
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    if audio_path.exists():
        audio_path.unlink()

    use_random = os.getenv("TTS_RANDOM_TEXT", "true").strip().lower() in {"1", "true", "yes"}
    if use_random:
        seed_env = os.getenv("TTS_RANDOM_SEED")
        seed = int(seed_env) if seed_env and seed_env.isdigit() else None
        say_text = random_paragraph(
            min_sentences=_int_env("TTS_RANDOM_MIN_SENTENCES", 5),
            max_sentences=_int_env("TTS_RANDOM_MAX_SENTENCES", 10),
            min_words=_int_env("TTS_RANDOM_MIN_WORDS", 5),
            max_words=_int_env("TTS_RANDOM_MAX_WORDS", 10),
            seed=seed,
        )
    else:
        say_text = Path("/ai/server/scripts/goodman.txt").read_text(encoding="utf-8").strip()

    print(f"📝 Text to synthesize ({len(say_text)} chars):\n{say_text}\n")

    query = urlencode(
        {
            "say": say_text,
            "max_chars": 4000,
            "expand": 0,
            "sentence_pause_ms": 500,
            "fade_ms": 10,
            "tail_ms": 40,
            "min_chunk_chars": 160,
            "min_chunk_words": 16,
            "gate_threshold_db": -80,
            "gate_ratio": 0.5,
            "gate_attack_ms": 4,
            "gate_release_ms": 400,
            "highpass_hz": 45,
            "crossfade_ms": 50,
            "target_peak": 0.60,
            "max_gain": 3.2
        }
    )

    url = f"http://localhost:9001/api/tts/stream?{query}"
    print(f"➡️  Request URL:\n{url}\n")

    with urlopen(url) as resp:
        print("📡 Response headers:")
        for k, v in resp.headers.items():
            print(f"  {k}: {v}")

        sr_hdr = resp.headers.get("X-Audio-Sample-Rate")
        if sr_hdr is None:
            print("⚠️  X-Audio-Sample-Rate header MISSING — defaulting to 22050")
            sr = 22050
        else:
            sr = int(sr_hdr)
            print(f"🔊 Using sample rate from header: {sr}")

        ffmpeg_proc = subprocess.Popen(
            [
                "ffmpeg",
                "-y",
                "-f", "s16le",
                "-ar", str(sr),
                "-ac", "1",
                "-i", "pipe:0",
                "-c:a", "pcm_s16le",
                str(audio_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        total_bytes = 0
        first_chunk = True

        try:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break

                if first_chunk:
                    print(f"📦 First audio chunk size: {len(chunk)} bytes")
                    first_chunk = False

                total_bytes += len(chunk)

                assert ffmpeg_proc.stdin is not None
                ffmpeg_proc.stdin.write(chunk)

        finally:
            if ffmpeg_proc.stdin:
                ffmpeg_proc.stdin.close()

        rc = ffmpeg_proc.wait()
        if rc != 0:
            raise subprocess.CalledProcessError(rc, ffmpeg_proc.args)

    print(f"\n📊 Total audio bytes written: {total_bytes}")
    print(f"✅ Wrote streaming TTS output to: {audio_path}")


if __name__ == "__main__":
    main()
