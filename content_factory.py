import os
import uuid
import datetime
import requests
import subprocess
import random
import sys
import traceback
import json
import logging
import yt_dlp
from groq import Groq
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Load .env file if exists (for local development)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, use system env vars only

# Set up logging profesional
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
)
logger = logging.getLogger(__name__)


class Config:
    """Manajemen Konfigurasi dan Environment Variables."""
    GROQ_API_KEY = os.getenv("GROQ_API_KEY")
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

    LLM_MODEL = "llama-3.3-70b-versatile"
    OUTPUT_DIR = "output"
    
    # Durasi default potong klip (detik)
    CLIP_DURATION = 30
    
    # Video quality settings
    MAX_HEIGHT = 720
    MAX_BITRATE = "2M"
    BUFFER_SIZE = "4M"

    @classmethod
    def validate(cls):
        missing = []
        if not cls.GROQ_API_KEY: missing.append("GROQ_API_KEY")
        if not cls.TELEGRAM_BOT_TOKEN: missing.append("TELEGRAM_BOT_TOKEN")
        if not cls.TELEGRAM_CHAT_ID: missing.append("TELEGRAM_CHAT_ID")
        if missing:
            raise ValueError(f"Environment Variable berikut belum diatur: {', '.join(missing)}")


class GamingSourceDatabase:
    """Mengelola daftar link video gaming yang ingin di-clip."""
    def __init__(self, filepath="gaming_db.json"):
        self.filepath = filepath

    def get_target_video(self) -> dict:
        """Mengambil satu target video gaming untuk diproses."""
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    db = json.load(f)
                    if db: return random.choice(db)
            except Exception as e:
                logger.error(f"Gagal membaca database JSON: {e}")
        
        # Fallback jika json kosong / belum dibuat - format baru (search_query + judul_game)
        return {
            "search_query": "mlbb best plays",
            "judul_game": "Mobile Legends"
        }


class TelegramNotifier:
    """Mengelola seluruh komunikasi dan log ke Telegram Bot."""
    def __init__(self):
        self.token = Config.TELEGRAM_BOT_TOKEN
        self.chat_id = Config.TELEGRAM_CHAT_ID
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    def send_message(self, text: str, parse_mode: str = None):
        try:
            url = f"{self.base_url}/sendMessage"
            payload = {"chat_id": self.chat_id, "text": text}
            if parse_mode: payload["parse_mode"] = parse_mode
            requests.post(url, data=payload, timeout=10)
        except Exception as e:
            logger.error(f"Gagal mengirim pesan Telegram: {e}")

    def send_video(self, video_path: str, caption: str):
        try:
            url = f"{self.base_url}/sendVideo"
            file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
            
            # Validasi ukuran file (Telegram bot limit 50MB)
            if file_size_mb > 49:
                self.send_message(f"⚠️ *Gagal Kirim:* Ukuran video ({file_size_mb:.2f} MB) melebihi batas limit bot Telegram (50 MB).")
                return False
            
            with open(video_path, 'rb') as f:
                files = {'video': f}
                data = {"chat_id": self.chat_id, "caption": caption, "parse_mode": "Markdown"}
                resp = requests.post(url, data=data, files=files, timeout=180)
            
            if resp.status_code == 200:
                logger.info(f"Klip Gaming sukses dikirim ke Telegram ({file_size_mb:.2f} MB).")
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
            logger.error(f"Gagal mengirim video ke Telegram: {e}")
            self.send_message(f"❌ *Error:* `{str(e)[:200]}`")
            return False


class VideoDownloaderManager:
    """Mengurus download video gaming dari YouTube secara efisien."""
    def __init__(self, ai_engine=None):
        self.temp_files = []
        self.ai_engine = ai_engine
        os.makedirs(Config.OUTPUT_DIR, exist_ok=True)

    def track_file(self, filepath: str):
        if filepath and filepath not in self.temp_files:
            self.temp_files.append(filepath)

    def cleanup(self, keep_file: str = None):
        logger.info("Memulai pembersihan file sampah render...")
        for path in self.temp_files:
            if path == keep_file: continue
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                logger.error(f"Gagal menghapus file {path}: {e}")
        self.temp_files.clear()

    def _build_ydl_opts(self, stage: str, cookie_path: str = None):
        """Build yt-dlp options with flexible format selector."""
        # Flexible format selector - avoids HLS, prioritizes native MP4
        format_selector = (
            f'bestvideo[height<={Config.MAX_HEIGHT}][ext=mp4][protocol!=m3u8]+bestaudio[ext=m4a]/'
            f'bestvideo[height<={Config.MAX_HEIGHT}][ext=mp4][protocol!=m3u8]+bestaudio/'
            f'bestvideo[height<={Config.MAX_HEIGHT}][vcodec^=avc1][protocol!=m3u8]+bestaudio[acodec^=mp4a]/'
            f'best[height<={Config.MAX_HEIGHT}][protocol!=m3u8]/'
            f'best[height<={Config.MAX_HEIGHT}]/'
            f'best'
        )
        
        base_opts = {
            'format': format_selector,
            'quiet': True,
            'nocheckcertificate': True,
            'socket_timeout': 60,
            'retries': 3,
            'ignoreerrors': False,
            'no_warnings': True,
            'extract_flat': False,
            'noplaylist': True,
            'http_chunk_size': 10485760,
        }
        
        if cookie_path and os.path.exists(cookie_path):
            base_opts['cookiefile'] = cookie_path
            logger.info("🍪 Menggunakan cookies YouTube untuk bypass bot detection")

        if stage == 'search':
            # Search phase: minimal format selector (just extract info, no download)
            base_opts.update({
                'format': 'best',  # Any format works for metadata extraction
                'default_search': 'ytsearch1',
                'skip_download': True,
            })
        elif stage == 'download':
            base_opts.update({})  # Will use _download_full_video + _trim_segment instead
        return base_opts

    def _download_full_video(self, url: str, cookie_path: str = None, start_time_sec: float = 0, duration: float = 30) -> tuple:
        """Download full video in MP4 format (no HLS) for reliable trimming."""
        temp_name = f"full_{uuid.uuid4().hex}.mp4"
        temp_path = os.path.join(Config.OUTPUT_DIR, temp_name)
        
        # First, get video info (duration) without downloading
        info_opts = {
            'quiet': True,
            'nocheckcertificate': True,
            'socket_timeout': 30,
            'retries': 2,
            'ignoreerrors': False,
            'no_warnings': True,
            'extract_flat': False,
            'noplaylist': True,
        }
        if cookie_path and os.path.exists(cookie_path):
            info_opts['cookiefile'] = cookie_path
        
        video_duration = 0
        try:
            with yt_dlp.YoutubeDL(info_opts) as ydl:
                video_info = ydl.extract_info(url, download=False)
                video_duration = video_info.get("duration", 0)
                logger.info(f"📊 Video duration: {video_duration}s")
        except Exception as e:
            logger.warning(f"⚠️ Could not get video duration: {e}")
        
        # Validate and adjust segment timing
        nonlocal_start = start_time_sec
        nonlocal_duration = duration
        if video_duration > 0:
            if nonlocal_start >= video_duration:
                nonlocal_start = max(0, video_duration - nonlocal_duration - 5)
                logger.warning(f"Start time melebihi durasi video, adjust ke {nonlocal_start}s")
            
            if nonlocal_start + nonlocal_duration > video_duration:
                nonlocal_duration = video_duration - nonlocal_start - 1
                logger.warning(f"Durasi dipotong ke {nonlocal_duration}s agar muat di video")
            
            if nonlocal_duration <= 0:
                logger.error("Durasi segment tidak valid")
                return None
        
        ydl_opts = {
            'format': (
                # Priority 1: MP4 non-HLS at 720p (ideal)
                'bestvideo[height<=720][ext=mp4][protocol!=m3u8]+bestaudio[ext=m4a]/'
                'bestvideo[height<=720][ext=mp4][protocol!=m3u8]+bestaudio/'
                # Priority 2: Any non-HLS at 720p (webm, etc.)
                'bestvideo[height<=720][protocol!=m3u8]+bestaudio/'
                # Priority 3: HLS at 720p (yt-dlp can handle now)
                'bestvideo[height<=720]+bestaudio/'
                # Priority 4: Any 720p
                'best[height<=720]/'
                # Priority 5: Best available
                'best'
            ),
            'outtmpl': temp_path,
            'quiet': False,  # Enable verbose output for debugging
            'nocheckcertificate': True,
            'socket_timeout': 60,
            'retries': 3,
            'ignoreerrors': False,
            'no_warnings': False,  # Show warnings for debugging
            'extract_flat': False,
            'noplaylist': True,
            'cookiefile': cookie_path if cookie_path and os.path.exists(cookie_path) else None,
            'http_chunk_size': 10485760,
        }
        
        logger.info(f"⬇️ Download full video (MP4, <=720p)...")
        logger.info(f"   URL: {url}")
        logger.info(f"   Cookie: {'Yes' if cookie_path else 'No'}")
        logger.info(f"   Trim: {nonlocal_start:.0f}s - {nonlocal_start + nonlocal_duration:.0f}s")
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception as e:
            logger.error(f"❌ Full video download error: {e}")
            return None
            
        if not os.path.exists(temp_path) or os.path.getsize(temp_path) < 100_000:
            logger.error("❌ Full video download failed or too small")
            return None
            
        logger.info(f"✅ Full video: {temp_name} ({os.path.getsize(temp_path)/1024/1024:.2f} MB)")
        self.track_file(temp_path)
        
        # Update start_time_sec and duration for trim phase
        return (temp_path, nonlocal_start, nonlocal_duration)

    def _trim_segment(self, input_path: str, start_time_sec: float, duration: float) -> str:
        """Trim segment using local ffmpeg - re-encode with ultrafast preset for compatibility."""
        output_name = f"segment_{uuid.uuid4().hex}.mp4"
        output_path = os.path.join(Config.OUTPUT_DIR, output_name)
        
        # Re-encode with ultrafast - compatible with moviepy, fast enough
        cmd = [
            'ffmpeg', '-y',
            '-ss', str(start_time_sec),
            '-i', input_path,
            '-t', str(duration),
            '-maxrate', Config.MAX_BITRATE,
            '-bufsize', Config.BUFFER_SIZE,
            '-c:v', 'libx264',
            '-preset', 'ultrafast',
            '-tune', 'fastdecode',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-movflags', '+faststart',
            output_path
        ]
        
        logger.info(f"✂️ Trimming (ultrafast): {start_time_sec:.0f}s - {start_time_sec + duration:.0f}s")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            if result.returncode != 0:
                logger.error(f"❌ FFmpeg trim error: {result.stderr[-500:]}")
                return None
        except subprocess.TimeoutExpired:
            logger.error("❌ FFmpeg trim timeout (90s)")
            return None
        except Exception as e:
            logger.error(f"❌ Trim error: {e}")
            return None
            
        if not os.path.exists(output_path) or os.path.getsize(output_path) < 100_000:
            logger.error("❌ Trim failed or output too small")
            return None
            
        logger.info(f"✅ Segment: {output_name} ({os.path.getsize(output_path)/1024/1024:.2f} MB)")
        self.track_file(output_path)
        return output_path
        if not os.path.exists(output_path) or os.path.getsize(output_path) < 100_000:
            logger.error("❌ Trim failed or output too small")
            return None
            
        logger.info(f"✅ Segment: {output_name} ({os.path.getsize(output_path)/1024/1024:.2f} MB)")
        self.track_file(output_path)
        return output_path

    def _download_subtitles(self, url: str, cookie_path: str = None) -> str:
        """Download subtitles/transcript from YouTube video."""
        logger.info("📝 Mengunduh subtitle/transcript...")
        temp_sub = os.path.join(Config.OUTPUT_DIR, f"sub_{uuid.uuid4().hex}.vtt")
        
        ydl_opts = {
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['id', 'en', 'en-US'],
            'subtitlesformat': 'vtt',
            'outtmpl': temp_sub.replace('.vtt', ''),
            'quiet': True,
            'nocheckcertificate': True,
            'socket_timeout': 30,
            'retries': 2,
            'ignoreerrors': False,
            'no_warnings': True,
            'extract_flat': False,
            'noplaylist': True,
        }
        if cookie_path and os.path.exists(cookie_path):
            ydl_opts['cookiefile'] = cookie_path
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception as e:
            logger.warning(f"⚠️ Subtitle download failed: {e}")
            return None
        
        # Find downloaded subtitle file
        for ext in ['.id.vtt', '.en.vtt', '.en-US.vtt', '.vtt']:
            sub_path = temp_sub.replace('.vtt', ext)
            if os.path.exists(sub_path):
                logger.info(f"✅ Subtitle ditemukan: {sub_path}")
                return sub_path
        
        logger.warning("⚠️ Tidak ada subtitle yang tersedia")
        return None

    def _parse_vtt_transcript(self, vtt_path: str) -> str:
        """Parse VTT file to plain text transcript."""
        try:
            with open(vtt_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Simple VTT parsing - remove timestamps and tags
            lines = content.split('\n')
            transcript_lines = []
            for line in lines:
                line = line.strip()
                if not line or line.startswith('WEBVTT') or '-->' in line or line.startswith('NOTE'):
                    continue
                # Remove inline tags like <00:00:00.000>
                import re
                line = re.sub(r'<\d{2}:\d{2}:\d{2}\.\d{3}>', '', line)
                line = re.sub(r'<[^>]+>', '', line)
                if line:
                    transcript_lines.append(line)
            
            return ' '.join(transcript_lines)
        except Exception as e:
            logger.error(f"❌ VTT parse error: {e}")
            return ""

    def download_youtube_segment(self, url: str, start_time_sec: float, duration: float) -> str:
        """Download YouTube video, extract highlights via AI, then clip best segment."""
        # Handle search query (if not a full URL)
        if not url.startswith('http'):
            search_query = url
            url = None
        else:
            search_query = None

        cookie_path = "youtube_cookies.txt" if os.path.exists("youtube_cookies.txt") else None

        # Phase 1: Search if needed
        if search_query:
            logger.info(f"🔍 Mencari: {search_query}")
            search_opts = {
                'default_search': 'ytsearch1',
                'skip_download': True,
                'playlistend': 1,
                'quiet': True,
                'nocheckcertificate': True,
                'socket_timeout': 30,
                'retries': 2,
                'ignoreerrors': False,
                'no_warnings': True,
                'noplaylist': True,
            }
            if cookie_path:
                search_opts['cookiefile'] = cookie_path
            try:
                with yt_dlp.YoutubeDL(search_opts) as ydl:
                    search_result = ydl.extract_info(search_query, download=False)

                if not search_result or "entries" not in search_result or not search_result["entries"]:
                    logger.error("❌ Tidak ada hasil pencarian")
                    return None

                video_info = search_result["entries"][0]
                url = video_info.get("webpage_url") or video_info.get("url")
                video_title = video_info.get("title", "Unknown")
                video_duration = video_info.get("duration", 0)

                logger.info(f"📺 Ditemukan: {video_title[:60]}... ({video_duration}s)")
                logger.info(f"   URL: {url}")

            except Exception as e:
                logger.error(f"❌ Search/extract error: {e}")
                return None

        # Phase 1.5: Download subtitles & AI Highlight Detection
        transcript = ""
        highlights = []
        
        if video_duration > 60:  # Only for videos longer than 1 minute
            vtt_path = self._download_subtitles(url, cookie_path)
            if vtt_path:
                transcript = self._parse_vtt_transcript(vtt_path)
                if transcript and len(transcript) > 200:  # Minimum transcript length
                    logger.info(f"📝 Transcript length: {len(transcript)} chars")
                    logger.info("🤖 Analisis highlight dengan AI...")
                    highlights = self.ai_engine.extract_highlights(
                        transcript, video_duration, Config.CLIP_DURATION, max_highlights=3
                    )
                    if highlights:
                        logger.info(f"✅ {len(highlights)} highlight ditemukan:")
                        for i, h in enumerate(highlights):
                            logger.info(f"   #{i+1} {h['start_sec']}s-{h['end_sec']}s (score: {h['score']}): {h['reason'][:80]}")
                # Cleanup subtitle file
                try:
                    if vtt_path and os.path.exists(vtt_path):
                        os.remove(vtt_path)
                except:
                    pass

        # Determine best segment
        if highlights:
            # Use best AI highlight
            best = highlights[0]
            start_time_sec = best["start_sec"]
            duration = best["duration"]
            logger.info(f"🎯 AI PILIH: {start_time_sec}s - {start_time_sec + duration}s (score: {best['score']})")
        else:
            # Fallback: random
            start_time_sec = random.uniform(60, min(300, video_duration - duration - 10))
            logger.info(f"🎲 FALLBACK random: {start_time_sec:.0f}s")

        # Validate segment timing
        if video_duration > 0:
            if start_time_sec >= video_duration:
                start_time_sec = max(0, video_duration - duration - 5)
            if start_time_sec + duration > video_duration:
                duration = video_duration - start_time_sec - 1
            if duration <= 0:
                logger.error("Durasi segment tidak valid")
                return None

        # Phase 2: Download full video (MP4, not HLS)
        download_result = self._download_full_video(url, cookie_path, start_time_sec, duration)
        if not download_result:
            return None

        full_video, adjusted_start, adjusted_duration = download_result

        # Phase 3: Trim segment locally
        segment = self._trim_segment(full_video, adjusted_start, adjusted_duration)
        return segment


class AIContentEngine:
    """Mesin AI untuk membuat deskripsi/caption konten dan detek highlight."""
    def __init__(self):
        self.client = Groq(api_key=Config.GROQ_API_KEY)

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=10),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type((requests.RequestException, Exception)),
        reraise=True
    )
    def generate_gaming_caption(self, game_name: str) -> str:
        """Membuat caption sosial media bertema gaming yang viral."""
        prompt = f"Buat caption pendek TikTok/Shorts yang seru dan relateable untuk video klip game {game_name}. Berikan hook menarik di awal, emoji gaming, dan 8 hashtag gaming viral seperti #gaming #shorts #fyp. Hanya keluarkan teks caption."
        response = self.client.chat.completions.create(
            model=Config.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=200,
        )
        return response.choices[0].message.content.strip()

    def extract_highlights(self, transcript: str, video_duration: int, clip_duration: int = 30, max_highlights: int = 3) -> list:
        """
        Analisis transcript dengan AI untuk menemukan segment paling engaging.
        Return: list of dict [{start_sec, end_sec, reason, score}]
        """
        # Truncate transcript if too long (Groq limit ~130k tokens, ~100k chars safe)
        max_chars = 80000
        if len(transcript) > max_chars:
            transcript = transcript[:max_chars] + "\n...[truncated]"

        prompt = f"""Analisis transcript video gaming berikut (durasi: {video_duration}s) dan temukan {max_highlights} segment paling MENARIK/CLIP-WORTHY untuk konten TikTok/Shorts/Reels gaming.

Kriteria segment bagus:
- Aksi intense (clutch, outplay, teamfight, ace, 1vX)
- Momen emosional (reaction, scream, laugh, surprise)
- Highlight skill (combo, mechanic, prediction, outsmart)
- Narasi menarik (storytelling, tips, funny moment)
- Duration target: {clip_duration} detik per clip

Transcript:
{transcript}

Format output HANYA JSON array (tanpa markdown):
[
  {{"start_sec": 125, "end_sec": 155, "reason": "Fanny 1v3 clutch di lord pit, reaction kaget", "score": 9.5}},
  {{"start_sec": 340, "end_sec": 370, "reason": "Ling 5-man ulti + quadra kill, teammate scream", "score": 9.2}}
]

Catatan:
- start_sec < end_sec, beda = {clip_duration} detik
- start_sec >= 0, end_sec <= {video_duration}
- Urutkan dari score tertinggi
- Hanya JSON, tanpa penjelasan tambahan"""

        response = self.client.chat.completions.create(
            model=Config.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1000,
        )
        
        import json
        try:
            content = response.choices[0].message.content.strip()
            # Extract JSON from response
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            highlights = json.loads(content.strip())
            
            # Validate highlights
            valid_highlights = []
            for h in highlights:
                if isinstance(h, dict) and all(k in h for k in ["start_sec", "end_sec", "reason", "score"]):
                    start = max(0, min(int(h["start_sec"]), video_duration - 1))
                    end = min(video_duration, max(start + 1, int(h["end_sec"])))
                    if end - start >= 5:
                        valid_highlights.append({
                            "start_sec": start,
                            "end_sec": end,
                            "duration": end - start,
                            "reason": h["reason"][:200],
                            "score": float(h["score"])
                        })
            
            # Sort by score descending
            valid_highlights.sort(key=lambda x: x["score"], reverse=True)
            return valid_highlights[:max_highlights]
            
        except Exception as e:
            logger.warning(f"⚠️ AI highlight extraction failed: {e}, fallback to random")
            return []


class VideoRenderer:
    @staticmethod
    def assemble_gaming_clip(raw_clip_path: str, game_name: str) -> str:
        """Crop 16:9 to 9:16 vertical using pure ffmpeg (no MoviePy)."""
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = os.path.join(Config.OUTPUT_DIR, f"clip_{game_name}_{timestamp}.mp4")
        
        logger.info("🎬 Memulai crop & kompresi vertikal (pure ffmpeg)...")
        
        # Get input video dimensions
        probe_cmd = [
            'ffprobe', '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height',
            '-of', 'csv=p=0',
            raw_clip_path
        ]
        try:
            result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                logger.error(f"❌ ffprobe error: {result.stderr}")
                return None
            w, h = map(int, result.stdout.strip().split(','))
        except Exception as e:
            logger.error(f"❌ Failed to get video dimensions: {e}")
            return None
        
        # Calculate crop for 16:9 -> 9:16 (center crop)
        # Target: 1080x1920 (9:16)
        # Scale height to 1920, then crop width to 1080 from center
        # ffmpeg filter: scale=-2:1920, crop=1080:1920:(ow-1080)/2:(oh-1920)/2
        vf_filter = (
            f"scale=-2:1920,"
            f"crop=1080:1920:(ow-1080)/2:(oh-1920)/2,"
            f"setsar=1"
        )
        
        cmd = [
            'ffmpeg', '-y',
            '-i', raw_clip_path,
            '-vf', vf_filter,
            '-maxrate', Config.MAX_BITRATE,
            '-bufsize', Config.BUFFER_SIZE,
            '-c:v', 'libx264',
            '-preset', 'ultrafast',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-movflags', '+faststart',
            '-pix_fmt', 'yuv420p',
            output_file
        ]
        
        logger.info(f"🎬 Crop 16:9→9:16 + compress: {w}x{h} → 1080x1920")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if result.returncode != 0:
                logger.error(f"❌ FFmpeg crop+compress error: {result.stderr[-500:]}")
                return None
        except subprocess.TimeoutExpired:
            logger.error("❌ FFmpeg crop+compress timeout (180s)")
            return None
        except Exception as e:
            logger.error(f"❌ Crop+compress error: {e}")
            return None
            
        if not os.path.exists(output_file) or os.path.getsize(output_file) < 100_000:
            logger.error("❌ Output failed or too small")
            return None
            
        logger.info(f"✅ Final clip: {os.path.basename(output_file)} ({os.path.getsize(output_file)/1024/1024:.2f} MB)")
        return output_file


class ContentPipeline:
    """Orkestrator Utama Clipper Otomatis Bersih."""
    def __init__(self):
        Config.validate()
        self.db = GamingSourceDatabase()
        self.notifier = TelegramNotifier()
        self.ai_engine = AIContentEngine()
        self.asset_mgr = VideoDownloaderManager(ai_engine=self.ai_engine)

    def run(self):
        video_final = None
        try:
            logger.info("🚀 Memulai Gaming Clipper Factory (Clean Mode)...")
            self.notifier.send_message("🚀 Sedang memotong klip gaming terbaru...")

            # 1. Ambil target video
            target = self.db.get_target_video()
            search_query = target["search_query"]
            game_title = target["judul_game"]

            # 2. Download & AI Highlight Detection → clip best segment
            # start_time_sec & duration akan di-determine oleh AI highlight detection di dalam
            raw_clip = self.asset_mgr.download_youtube_segment(
                search_query, 0, Config.CLIP_DURATION
            )

            if not raw_clip:
                raise ValueError("Gagal mengunduh potongan video dari YouTube.")

            # 3. Merakit video (Crop ke vertikal secara bersih)
            video_final = VideoRenderer.assemble_gaming_clip(raw_clip, game_title)

            # 4. Buat Caption dengan Groq LLM
            caption = self.ai_engine.generate_gaming_caption(game_title)

            # 5. Kirim ke Telegram
            telegram_caption = (
                f"🎮 *KLIP GAMING OTOMATIS SIAP UPLOAD*\n\n"
                f"🎮 *Game:* {game_title}\n"
                f"🔍 *Query:* {search_query}\n\n"
                f"📝 *Caption Konten:*\n`{caption}`"
            )
            self.notifier.send_video(video_final, telegram_caption)
            self.notifier.send_message("✅ Sukses memproses klip gaming!")

        except Exception as e:
            error_details = traceback.format_exc()
            logger.critical(f"Sistem Crash: {e}\n{error_details}")
            self.notifier.send_message(f"❌ *Clipper Gagal!* Terjadi error:\n`{str(e)}`")
            sys.exit(1)
            
        finally:
            self.asset_mgr.cleanup(keep_file=video_final)


if __name__ == "__main__":
    factory = ContentPipeline()
    factory.run()