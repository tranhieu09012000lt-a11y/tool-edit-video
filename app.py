"""
Video Filter Studio — phiên bản Streamlit + FFmpeg (server-side)
------------------------------------------------------------------
Khác với bản React chạy trong trình duyệt, bản này CẦN chạy trên máy/server
có cài sẵn Python và FFmpeg. Lý do cần bản này: một số video tải từ
TikTok/Douyin dùng codec (ví dụ AV1) mà Safari trên iOS không giải mã được.
Cách duy nhất để "sửa" video đó là ép chuyển mã (transcode) thật sự bằng
FFmpeg sang H.264 + AAC trước khi phát — việc này không thể làm được chỉ
bằng JavaScript trong trình duyệt.

Cách chạy:
    1. Cài FFmpeg (xem README.md).
    2. pip install -r requirements.txt
    3. streamlit run app.py
"""

import json
import os
import shutil
import subprocess
import tempfile
import time

import streamlit as st

st.set_page_config(page_title="Video Filter Studio (FFmpeg)", layout="wide")

# ------------------------------------------------------------------
# Hằng số & tiện ích
# ------------------------------------------------------------------
LARGE_PIXELS = 1920 * 1080 * 1.15
LARGE_DURATION_SEC = 12 * 60
MAX_UPLOAD_MB = 500  # cảnh báo mềm, không chặn cứng


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def probe_video(path: str) -> dict:
    """Đọc width/height/duration/codec bằng ffprobe. Trả về dict, ném lỗi nếu file hỏng."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,codec_name,r_frame_rate",
        "-show_entries", "format=duration",
        "-of", "json",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe không đọc được file: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    if not streams:
        raise RuntimeError("Không tìm thấy luồng video hợp lệ trong file này.")
    stream = streams[0]
    fmt = data.get("format", {})
    return {
        "width": int(stream.get("width", 0)),
        "height": int(stream.get("height", 0)),
        "codec": stream.get("codec_name", "?"),
        "duration": float(fmt.get("duration", 0) or 0),
    }


def build_filter_chain(width: int, height: int, flip: bool, zoom_pct: int,
                        hue_deg: int, sat_pct: int, border_px: int, border_color: str) -> str:
    """
    Xây chuỗi -vf FFmpeg tương ứng chính xác các control của bản React:
      - Zoom (100-150%): crop quanh tâm rồi scale lại về đúng kích thước gốc
        (tương đương phép "scale quanh tâm" trong canvas).
      - Lật ngang: hflip.
      - Hue/Saturation: filter `hue` (đơn vị độ và hệ số nhân, đổi 0-200% -> 0-2.0).
      - Khung viền: `drawbox` vẽ SAU CÙNG, đúng mép khung hình gốc, không bị
        ảnh hưởng bởi zoom/hue — giống cách vẽ trong bản canvas.
    """
    filters = []

    zoom = max(1.0, zoom_pct / 100.0)
    if zoom > 1.0001:
        crop_w = max(2, int(width / zoom) & ~1)   # số chẵn để tránh lỗi encode
        crop_h = max(2, int(height / zoom) & ~1)
        offset_x = (width - crop_w) // 2
        offset_y = (height - crop_h) // 2
        filters.append(f"crop={crop_w}:{crop_h}:{offset_x}:{offset_y}")
        filters.append(f"scale={width}:{height}")

    if flip:
        filters.append("hflip")

    if hue_deg != 0 or sat_pct != 100:
        sat_mult = max(0.0, sat_pct / 100.0)
        filters.append(f"hue=h={hue_deg}:s={sat_mult}")

    if border_px > 0:
        # drawbox vẽ khung rỗng bám mép khung hình thật (0,0,width,height).
        color = border_color.lstrip("#")
        filters.append(f"drawbox=x=0:y=0:w={width}:h={height}:color=0x{color}:t={border_px}")

    return ",".join(filters) if filters else "null"


def run_ffmpeg_transcode(input_path: str, output_path: str, vf_chain: str,
                          progress_cb=None, duration: float = 0):
    """
    Chuyển mã theo đúng yêu cầu:
      - video: libx264, profile high, level 4.0
      - audio: aac, 128k, 44100Hz
      - container: mp4 với -movflags +faststart (đưa moov atom lên đầu file
        để iOS Safari có thể bắt đầu phát ngay, không cần tải hết file).
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", vf_chain,
        "-c:v", "libx264",
        "-profile:v", "high",
        "-level", "4.0",
        "-pix_fmt", "yuv420p",  # bắt buộc để iOS Safari chắc chắn giải mã được
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        "-nostats",
        output_path,
    ]
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
    )
    for line in process.stdout:
        if progress_cb and duration > 0 and line.startswith("out_time_ms="):
            try:
                out_time_ms = int(line.strip().split("=")[1])
                progress_cb(min(1.0, (out_time_ms / 1_000_000) / duration))
            except (ValueError, IndexError):
                pass
    stderr_output = process.stderr.read()
    process.wait()
    if process.returncode != 0:
        raise RuntimeError(f"FFmpeg báo lỗi:\n{stderr_output[-2000:]}")


# ------------------------------------------------------------------
# Giao diện
# ------------------------------------------------------------------
st.title("🎛️ Video Filter Studio — bản FFmpeg (tương thích iOS)")
st.caption(
    "Bản này chuyển mã thật sự bằng FFmpeg sang H.264 + AAC + faststart trước khi phát, "
    "để sửa lỗi video từ TikTok/Douyin (thường dùng AV1) không phát được trên Safari/iOS."
)

if not ffmpeg_available():
    st.error(
        "Không tìm thấy FFmpeg/FFprobe trên máy này. Cài đặt trước khi chạy tiếp — "
        "xem hướng dẫn trong README.md đi kèm."
    )
    st.stop()

if "reset_key" not in st.session_state:
    st.session_state.reset_key = 0

uploaded = st.file_uploader(
    "Tải video lên (mp4, mov, webm...)",
    type=None,
    help="Chấp nhận bất kỳ định dạng nào FFmpeg đọc được, kể cả file lỗi/không chuẩn từ app tải video mạng xã hội.",
)

if uploaded is None:
    st.info("Chưa có video nào được tải lên.")
    st.stop()

if uploaded.size > MAX_UPLOAD_MB * 1024 * 1024:
    st.warning(
        f"File khá lớn ({uploaded.size / 1024 / 1024:.0f} MB). Việc chuyển mã có thể mất nhiều thời gian."
    )

# Lưu file gốc vào thư mục tạm riêng cho phiên làm việc này
work_dir = tempfile.mkdtemp(prefix="vfs_")
input_path = os.path.join(work_dir, "input_" + uploaded.name)
with open(input_path, "wb") as f:
    f.write(uploaded.getbuffer())

try:
    info = probe_video(input_path)
except RuntimeError as e:
    st.error(
        f"Không đọc được video này (có thể file bị hỏng hoặc không phải video hợp lệ). Chi tiết: {e}"
    )
    shutil.rmtree(work_dir, ignore_errors=True)
    st.stop()

width, height, duration, codec = info["width"], info["height"], info["duration"], info["codec"]

st.caption(f"📐 {width}×{height} px · 🎞️ codec gốc: `{codec}` · ⏱️ {duration:.1f}s")

if width * height > LARGE_PIXELS or duration > LARGE_DURATION_SEC:
    st.warning("Video khá lớn/dài — quá trình chuyển mã có thể chậm trên máy cấu hình trung bình.")

col_preview, col_controls = st.columns([2, 1])

with col_controls:
    st.subheader("Bộ lọc")
    if st.button("↺ Đặt lại bộ lọc về mặc định"):
        st.session_state.reset_key += 1

    rk = st.session_state.reset_key
    flip = st.checkbox("Lật ngang (mirror)", value=False, key=f"flip_{rk}")
    zoom = st.slider("Phóng to (%)", 100, 150, 105, key=f"zoom_{rk}")
    hue = st.slider("Tông màu / hue (độ)", -180, 180, 0, key=f"hue_{rk}")
    sat = st.slider("Độ bão hoà / saturation (%)", 0, 200, 100, key=f"sat_{rk}")
    border_w = st.slider("Độ dày khung viền (px)", 0, 40, 0, key=f"bw_{rk}")
    border_color = st.color_picker("Màu khung viền", "#e7a23a", key=f"bc_{rk}")

    st.divider()
    run_export = st.button("⭳ Chuyển mã & Xuất video (H.264/AAC, tương thích iOS)", type="primary")

vf_chain = build_filter_chain(width, height, flip, zoom, hue, sat, border_w, border_color)

with col_preview:
    st.subheader("Video gốc (chưa chuyển mã)")
    st.video(input_path)
    st.caption(
        "Nếu video gốc không phát được ở trên (ví dụ do AV1), đây chính là bằng chứng cho lỗi codec — "
        "hãy nhấn nút chuyển mã bên phải."
    )

if run_export:
    output_path = os.path.join(work_dir, "output.mp4")
    progress_bar = st.progress(0.0, text="Đang chuyển mã bằng FFmpeg…")

    def on_progress(fraction: float):
        progress_bar.progress(fraction, text=f"Đang chuyển mã bằng FFmpeg… {int(fraction * 100)}%")

    try:
        start = time.time()
        run_ffmpeg_transcode(input_path, output_path, vf_chain, on_progress, duration)
        elapsed = time.time() - start
        progress_bar.progress(1.0, text=f"Hoàn tất trong {elapsed:.1f}s")
    except RuntimeError as e:
        st.error(str(e))
        st.stop()

    with open(output_path, "rb") as f:
        video_bytes = f.read()

    st.success("Chuyển mã xong — video dưới đây đã ở định dạng H.264/AAC/MP4 tương thích iOS Safari.")
    st.video(video_bytes, format="video/mp4")
    st.download_button(
        "Tải video đã xuất (.mp4)",
        data=video_bytes,
        file_name=f"video-filter-studio-{int(time.time())}.mp4",
        mime="video/mp4",
    )

    st.caption(
        "Thông số xuất: codec video libx264 (profile High, level 4.0), pix_fmt yuv420p, "
        "codec âm thanh AAC 128kbps/44.1kHz, container MP4 với -movflags +faststart "
        "(đưa moov atom lên đầu file để phát được ngay trên iOS mà không cần tải hết file)."
    )

    # Dọn dẹp file output sau khi đã đọc vào bộ nhớ để không tích tệp rác
    try:
        os.remove(output_path)
    except OSError:
        pass
