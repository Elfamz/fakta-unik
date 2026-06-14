#!/usr/bin/env python3
"""
Automated Gaming Clip Factory - Production Ready
=================================================
Fully automated pipeline: YouTube search → download segment → 9:16 vertical → AI caption → Telegram delivery
"""

import os
import json
import random
import logging
import tempfile
import subprocess
import shutil
from pathlib import Path
from typing import Optional, Dict, List

import requests
import yt_dlp
from groq import Groq
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# ============================
# CONFIGURATION
# ============================
class Config:
    GROQ_API_KEY = os.getenv("GROQ_API_KEY")
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

    LLM_MODEL = "llama-3.3-70b-versatile"
    OUTPUT_DIR = "output"
    CLIP_DURATION = 30  # seconds
    MAX_VIDEO_SIZE_MB = 49  # Telegram bot API limit
    MAX_BITRATE = "2M"      # FFmpeg maxrate
    BUFFER_SIZE = "4M"      # FFmpeg bufsize

    # YouTube search config
    MAX_SEARCH_RESULTS = 1
    VIDEO_QUALITY = "480p"
    SEARCH_LANG = "id"      # Indonesian content preference

    @classmethod
    def validate(cls):
        missing = []
        if not cls.GROQ_API_KEY: missing.append("GROQ_API_KEY")
        if not cls.TELEGRAM_BOT_TOKEN: missing.append("TELEGRAM_BOT_TOKEN")
        if not cls.TELEGRAM_CHAT_ID: missing.append("TELEGRAM_CHAT_ID")
        if missing:
            raise ValueError(f"Environment Variable berikut belum diatur: {', '.join(missing)}")

    @classmethod
    def ensure_dirs(cls):
        Path(cls.OUTPUT_DIR).mkdir(parents=True, exist_ok=True)


# ============================
# LOGGING SETUP
# ============================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("GamingClipFactory")

# Suppress noisy loggers
logging.getLogger("groq").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("yt_dlp").setLevel(logging.WARNING)


# ============================
# TELEGRAM NOTIFIER
# ============================
class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    def send_message(self, text: str, parse_mode: str = "Markdown") -> bool:
        try:
            url = f"{self.base_url}/sendMessage"
            payload = {"chat_id": self.chat_id, "text": text, "parse_mode": parse_mode}
            resp = requests.post(url, json=payload, timeout=10)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Telegram send_message error: {e}")
            return False

    def send_video(self, video_path: str, caption: str) -> bool:
        try:
            file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
            if file_size_mb > Config.MAX_VIDEO_SIZE_MB:
                self.send_message(
                    f"⚠️ *Gagal Kirim:* Ukuran video ({file_size_mb:.2f} MB) "
                    f"melebihi batas limit bot Telegram ({Config.MAX_VIDEO_SIZE_MB} MB)."
                )
                return False

            url = f"{self.base_url}/sendVideo"
            with open(video_path, "rb") as f:
                files = {"video": f}
                data = {
                    "chat_id": self.chat_id,
                    "caption": caption,
                    "parse_mode": "Markdown",
                    "supports_streaming": True
                }
                resp = requests.post(url, data=data, files=files, timeout=180)

            if resp.status_code == 200:
                logger.info(f"✅ Video terkirim ke Telegram ({file_size_mb:.2f} MB)")
                return True
            else:
                logger.error(f"Telegram API error: {resp.status_code} - {resp.text}")
                self.send_message(f"❌ *Gagal kirim video:* HTTP {resp.status_code}")
                return False

        except requests.Timeout:
            logger.error("Telegram upload timeout (180s)")
            self.send_message("⏱ *Timeout:* Upload video melebihi 3 menit.")
            return False
        except Exception as e:
            logger.error(f"Telegram send_video error: {e}")
            self.send_message(f"❌ *Error:* `{str(e)[:200]}`")
            return False


# ============================
# GAMING SOURCE DATABASE
# ============================
class GamingSourceDatabase:
    def __init__(self, filepath: str = "gaming_db.json"):
        self.filepath = filepath

    def get_target_video(self) -> Dict:
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    db = json.load(f)
                if db and isinstance(db, list):
                    chosen = random.choice(db)
                    logger.info(f"🎯 Pilih dari database: {chosen['judul_game']} - {chosen['search_query']}")
                    return chosen
            except Exception as e:
                logger.error(f"Gagal membaca gaming_db.json: {e}")

        # Fallback
        logger.warning("Menggunakan fallback query (database tidak ditemukan/empty)")
        return {
            "search_query": "gaming montage shorts",
            "judul_game": "Gaming"
        }


# ============================
# VIDEO DOWNLOADER MANAGER
# ============================
class VideoDownloaderManager:
    def __init__(self, output_dir: str = "output"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.temp_files: List[str] = []

    def track_file(self, filepath: str):
        if filepath and filepath not in self.temp_files:
            self.temp_files.append(filepath)

    def cleanup(self, keep_file: Optional[str] = None):
        for path in self.temp_files:
            if path == keep_file:
                continue
            if os.path.exists(path):
                try:
                    os.remove(path)
                    logger.debug(f"🗑 Cleaned: {path}")
                except Exception as e:
                    logger.warning(f"Gagal hapus temp file {path}: {e}")
        self.temp_files.clear()

    def _build_ydl_opts(self, stage: str, cookie_path: Optional[str] = None) -> Dict:
        """Build yt-dlp options for search or download stage."""
        base_opts = {
            "format": f"bestvideo[height<={Config.VIDEO_QUALITY[:-1]}][ext=mp4]+bestaudio[ext=m4a]/best[height<={Config.VIDEO_QUALITY[:-1]}]",
            "quiet": True,
            "nocheckcertificate": True,
            "ignoreerrors": False,
            "no_warnings": True,
            "extract_flat": False,
        }

        if cookie_path and os.path.exists(cookie_path):
            base_opts["cookiefile"] = cookie_path
            logger.info("🍪 Menggunakan cookies YouTube untuk bypass bot detection")

        if stage == "search":
            return {
                **base_opts,
                "default_search": f"ytsearch{Config.MAX_SEARCH_RESULTS}",
                "skip_download": True,
            }
        elif stage == "download":
            return {
                **base_opts,
                "external_downloader": "ffmpeg",
                "external_downloader_args": {
                    "args": []  # Will be set per-call
                },
            }
        return base_opts

    def download_segment(self, search_query: str, start_time_sec: float, duration: float) -> Optional[str]:
        """
        Two-phase download: search → extract URL → download segment with ffmpeg.
        Returns path to downloaded segment or None on failure.
        """
        cookie_path = "youtube_cookies.txt" if os.path.exists("youtube_cookies.txt") else None

        # Phase 1: Search
        logger.info(f"🔍 Mencari: {search_query}")
        search_opts = self._build_ydl_opts("search", cookie_path)
        try:
            with yt_dlp.YoutubeDL(search_opts) as ydl:
                search_result = ydl.extract_info(search_query, download=False)

            if not search_result or "entries" not in search_result or not search_result["entries"]:
                logger.error("❌ Tidak ada hasil pencarian")
                return None

            video_info = search_result["entries"][0]
            video_url = video_info.get("url") or video_info.get("webpage_url")
            video_title = video_info.get("title", "Unknown")
            video_duration = video_info.get("duration", 0)

            logger.info(f"📺 Ditemukan: {video_title[:60]}... ({video_duration}s)")

            # Validate segment timing
            if start_time_sec >= video_duration:
                start_time_sec = max(0, video_duration - duration - 5)
                logger.warning(f"Start time melebihi durasi video, adjust ke {start_time_sec}s")

            if start_time_sec + duration > video_duration:
                duration = video_duration - start_time_sec - 1
                logger.warning(f"Durasi dipotong ke {duration}s agar muat di video")

            if duration <= 0:
                logger.error("Durasi segment tidak valid")
                return None

        except Exception as e:
            logger.error(f"❌ Search/extract error: {e}")
            return None

        # Phase 2: Download segment
        output_filename = f"segment_{random.randint(10000, 99999)}.mp4"
        output_path = str(self.output_dir / output_filename)

        download_opts = self._build_ydl_opts("download", cookie_path)
        download_opts["outtmpl"] = output_path
        download_opts["external_downloader_args"]["args"] = [
            "-ss", str(start_time_sec),
            "-t", str(duration),
            "-maxrate", Config.MAX_BITRATE,
            "-bufsize", Config.BUFFER_SIZE,
            "-c:v", "libx264",
            "-preset", "fast",
            "-c:a", "aac",
            "-b:a", "128k"
        ]

        logger.info(f"⬇️ Download segment: {start_time_sec:.0f}s - {start_time_sec + duration:.0f}s")

        try:
            with yt_dlp.YoutubeDL(download_opts) as ydl:
                ydl.download([video_url])
        except Exception as e:
            logger.error(f"❌ Download error: {e}")
            if os.path.exists(output_path):
                os.remove(output_path)
            return None

        # Verify output
        if not os.path.exists(output_path):
            logger.error("❌ File output tidak terbuat")
            return None

        file_size = os.path.getsize(output_path)
        if file_size < 100_000:  # Less than 100KB = probably corrupted
            logger.error(f"❌ File terlalu kecil ({file_size} bytes) - corrupt?")
            os.remove(output_path)
            return None

        logger.info(f"✅ Segment tersimpan: {output_filename} ({file_size / 1024 / 1024:.2f} MB)")
        self.track_file(output_path)
        return output_path


# ============================
# AI CONTENT ENGINE (GROQ)
# ============================
class AIContentEngine:
    def __init__(self, api_key: str, model: str = Config.LLM_MODEL):
        self.client = Groq(api_key=api_key)
        self.model = model

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type((requests.RequestException, Exception)),
        reraise=True
    )
    def generate_gaming_caption(self, game_name: str) -> str:
        prompt = (
            f"Buat caption pendek TikTok/Shorts yang seru dan relateable untuk video klip game {game_name}. "
            f"Berikan hook menarik di awal, emoji gaming, dan 8 hashtag gaming viral seperti #gaming #shorts #fyp. "
            f"Hanya keluarkan teks caption saja, bahasa Indonesia."
        )

        logger.info(f"🤖 Generate caption untuk: {game_name}")
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=200,
            )
            caption = response.choices[0].message.content.strip()
            logger.info(f"✅ Caption generated ({len(caption)} chars)")
            return caption
        except Exception as e:
            logger.error(f"❌ Groq API error: {e}")
            raise


# ============================
# VIDEO RENDERER (9:16 VERTICAL)
# ============================
class VideoRenderer:
    def __init__(self, output_dir: str = "output"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def check_ffmpeg(self) -> bool:
        return shutil.which("ffmpeg") is not None

    def render_vertical(self, input_path: str, output_filename: Optional[str] = None) -> Optional[str]:
        """Convert video to 9:16 vertical format with crop + blur background."""
        if not self.check_ffmpeg():
            logger.error("❌ FFmpeg tidak ditemukan")
            return None

        if not output_filename:
            base = Path(input_path).stem
            output_filename = f"{base}_vertical.mp4"

        output_path = str(self.output_dir / output_filename)

        # FFmpeg filter: crop to 9:16, blur background, center original
        filter_complex = (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,"
            "gblur=sigma=20[bg];"
            "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-filter_complex", filter_complex,
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            output_path
        ]

        logger.info(f"🎞 Render vertical 9:16: {Path(input_path).name} → {output_filename}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                logger.error(f"❌ FFmpeg error: {result.stderr[-500:]}")
                return None

            if not os.path.exists(output_path):
                logger.error("❌ Output file tidak terbuat")
                return None

            file_size = os.path.getsize(output_path)
            logger.info(f"✅ Render selesai: {output_filename} ({file_size / 1024 / 1024:.2f} MB)")
            return output_path

        except subprocess.TimeoutExpired:
            logger.error("❌ FFmpeg timeout (5 menit)")
            return None
        except Exception as e:
            logger.error(f"❌ Render error: {e}")
            return None


# ============================
# MAIN ORCHESTRATOR
# ============================
class GamingClipFactory:
    def __init__(self):
        Config.validate()
        Config.ensure_dirs()

        self.db = GamingSourceDatabase()
        self.downloader = VideoDownloaderManager(Config.OUTPUT_DIR)
        self.ai_engine = AIContentEngine(Config.GROQ_API_KEY, Config.LLM_MODEL)
        self.renderer = VideoRenderer(Config.OUTPUT_DIR)
        self.telegram = TelegramNotifier(Config.TELEGRAM_BOT_TOKEN, Config.TELEGRAM_CHAT_ID)

        # Notify start
        self.telegram.send_message("🚀 *Gaming Clip Factory Started*\nMencari konten gaming...")

    def run(self) -> bool:
        """Main pipeline execution. Returns True on success."""
        final_video = None
        try:
            # 1. Get target game/video
            target = self.db.get_target_video()
            search_query = target["search_query"]
            game_title = target["judul_game"]

            # 2. Random segment timing (60-240s into video for variety)
            start_time = random.uniform(60, 240)
            duration = Config.CLIP_DURATION

            # 3. Download segment
            segment_path = self.downloader.download_segment(search_query, start_time, duration)
            if not segment_path:
                self.telegram.send_message("❌ *Gagal:* Tidak bisa download segment video")
                return False

            # 4. Render to vertical 9:16
            vertical_path = self.renderer.render_vertical(segment_path)
            if not vertical_path:
                self.telegram.send_message("❌ *Gagal:* Render vertical gagal")
                return False

            # 5. Generate AI caption
            caption = self.ai_engine.generate_gaming_caption(game_title)
            full_caption = f"{caption}\n\n🎮 Game: {game_title}\n#gaming #shorts #fyp #viral #gamer"

            # 6. Send to Telegram
            success = self.telegram.send_video(vertical_path, full_caption)

            if success:
                self.telegram.send_message("✅ *Selesai:* Klip gaming berhasil dikirim!")
                final_video = vertical_path
                return True
            else:
                return False

        except Exception as e:
            logger.exception("Pipeline error")
            self.telegram.send_message(f"❌ *Pipeline Error:* `{str(e)[:300]}`")
            return False

        finally:
            # Cleanup temp files, keep final video if needed for debugging
            self.downloader.cleanup(keep_file=final_video)


# ============================
# ENTRY POINT
# ============================
def main():
    print("=" * 50)
    print("🎮 GAMING CLIP FACTORY")
    print("=" * 50)

    try:
        factory = GamingClipFactory()
        success = factory.run()
        exit_code = 0 if success else 1
        print(f"\n{'✅ SUCCESS' if success else '❌ FAILED'}")
        return exit_code

    except ValueError as e:
        logger.error(f"Config error: {e}")
        print(f"❌ CONFIG ERROR: {e}")
        return 1
    except Exception as e:
        logger.exception("Fatal error")
        print(f"❌ FATAL ERROR: {e}")
        return 1


if __name__ == "__main__":
    exit(main())