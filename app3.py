import os
import io
from pathlib import Path
from typing import List
from dotenv import load_dotenv
from PIL import Image
import streamlit as st
import streamlit.components.v1 as components

import dropbox
from dropbox.files import WriteMode, UploadSessionCursor, CommitInfo
from dropbox.exceptions import ApiError
from openai import OpenAI
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from rembg import remove  # 추가

# ========== CONFIG ==========
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")

client = OpenAI(api_key=OPENAI_API_KEY)

# ========== FILE UTILS ==========
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png"}

def is_image_file(file: Path) -> bool:
    return file.suffix.lower().lstrip(".") in IMAGE_EXTENSIONS

def split_filename(file: Path) -> tuple[str, str]:
    return file.stem, file.suffix.lstrip(".")

def find_asset_for_image(image_file: Path, assets_dir: Path | None = None) -> Path | None:
    if assets_dir is None:
        assets_dir = image_file.parent
    for ext in (".sbsar", ".zip"):
        candidate = assets_dir / f"{image_file.stem}{ext}"
        if candidate.exists():
            return candidate
    return None

# ========== DROPBOX UTILS ==========
def get_dropbox_client() -> dropbox.Dropbox:
    return dropbox.Dropbox(
        oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
        app_key=DROPBOX_APP_KEY,
        app_secret=DROPBOX_APP_SECRET,
    )

def resolve_unique_dropbox_path(dbx, base_path: str, ext: str, max_tries: int = 1000) -> str:
    counter = 0
    path = f"{base_path}.{ext}"
    while counter < max_tries:
        try:
            dbx.files_get_metadata(path)
            counter += 1
            path = f"{base_path}_{counter}.{ext}"
        except ApiError as e:
            try:
                if e.error.is_path() and e.error.get_path().is_not_found():
                    return path
            except Exception:
                return path
    raise RuntimeError("resolve_unique_dropbox_path: too many conflicts, aborting.")

def upload_with_chunks(dbx_client, data: bytes, dropbox_path: str):
    total = len(data)
    chunk_size = 4 * 1024 * 1024
    if total <= chunk_size:
        dbx_client.files_upload(data, dropbox_path, mode=WriteMode.add)
        return
    session = dbx_client.files_upload_session_start(data[:chunk_size])
    offset = chunk_size
    commit = CommitInfo(path=dropbox_path, mode=WriteMode.add)
    while offset < total:
        end = min(offset + chunk_size, total)
        chunk = data[offset:end]
        cursor = UploadSessionCursor(session_id=session.session_id, offset=offset)
        if end < total:
            dbx_client.files_upload_session_append_v2(chunk, cursor)
            offset = end
        else:
            dbx_client.files_upload_session_finish(chunk, cursor, commit)
            break

def get_or_create_shared_link(dbx, path: str) -> str:
    links = dbx.sharing_list_shared_links(path=path, direct_only=True).links
    if links:
        return links[0].url
    else:
        return dbx.sharing_create_shared_link_with_settings(path).url

# ========== GPT SUMMARY ==========
def convert_dropbox_urls(original_url: str) -> tuple[str, str]:
    parsed = urlparse(original_url)
    if 'dropbox.com' not in parsed.netloc:
        return original_url, original_url
    base = parsed._replace(query="")
    raw_qs = parse_qs(parsed.query)
    raw_qs.pop('dl', None)
    raw_qs['raw'] = ['1']
    raw_url = urlunparse(base._replace(query=urlencode(raw_qs, doseq=True)))
    dl_qs = parse_qs(parsed.query)
    dl_qs.pop('raw', None)
    dl_qs['dl'] = ['1']
    download_url = urlunparse(base._replace(query=urlencode(dl_qs, doseq=True)))
    return raw_url, download_url

def generate_image_summary(image_url: str, model: str = "gpt-4o") -> str:
    raw_url, _ = convert_dropbox_urls(image_url)
    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0.5,
            messages=[
                {
                    "role": "system",
                    "content": (
    "당신은 SEO 최적화에 능한 마케팅 카피라이터입니다. 이미지를 보고 웹에서 사용할 디지털 에셋 설명을 생성해야 합니다.\n\n"
    "아래 구조로 실제 설명을 출력하세요:\n"
    "<div class=\"desc\">\n"
    "  <div class=\"detail kr line-control\">\n"
    "    <h2>한국어 제목</h2>\n"
    "    한국어로 된 400자 내외의 설명. 형태/색상/대상의 특징을 주로 묘사하며 추가적으로 어떤것을 제작할 때 유용하다식의 설명 (예측용도 포함, 필수포함 단어 : 섭스턴스 페인터용 매터리얼, 무료 텍스쳐,무료다운로드)\n"
    "  </div>\n"
    "  <div class=\"detail en line-control\">\n"
    "    <h2>English Title</h2>\n"
    "    English description, about 300 characters(Including for predictive purposes,Required words: free texture, substance painter, free download)\n"
    "  </div>\n"
    "  <div class=\"HiddenInfoForSearch\" style=\"display:none\">\n"    
    "    롱테일키워드가 필요한 부분이야. 쉐이더 형태를 최대한 자세하게 묘사하고 이부분은 소프트웨어나 용도에 대한 설명을 자제해. 그냥 형태나 색감에 대해서만 이야기하라고 500자) \n"
    "  </div>\n"    
    "</div>\n\n"
    "4. 마지막에는 JSON-LD `<script>` 태그로 구조화 데이터를 생성합니다.\n"
    "- 필드는 다음과 같습니다. 단 타입은 반드시"ImageObject"로 표기: `@context`, `@type`, `name`, `description`\n"
    "- `image`, `url`, `offers` 필드는 값을 알 수 없으면 **아예 생략하십시오**\n"
    "- 출력할 때 `<script type=\"application/ld+json\">{...}</script>` 전체를 포함해야 합니다\n\n"
    "5. 디지털아트, 완벽합니다, 자랑합니다 등의 추상적/감상적인 표현은 절대 사용하지 마세요. 이 이미지는이 아니라 동그란 물체는 쉐이더라고 지칭하세요."
  )
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "1. 이 이미지를 보고 SEO에 최적화된 설명을 생성해주세요.\n"
                                "2. 구글 검색 최적화를 위한 JSON-LD도 <script></script> 태그 안에 포함해줘.\n"
                            )
                        },
                        {
                            "type": "image_url",
                            "image_url": { "url": raw_url }
                        }
                    ]
                }
            ],
            max_tokens=900
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"[요약 실패: {str(e)}]"

# ========== UI ==========
def convert_dropbox_url(shared_url: str, param: str) -> str:
    for old in ['?dl=0', '&dl=0']:
        if old in shared_url:
            return shared_url.replace(old, old[0] + param)
    sep = '&' if '?' in shared_url else '?'
    return f"{shared_url}{sep}{param}"

def generate_html_snippet(asset_link: str, summary: str) -> str:
    return f'''<div class="info" style="display:none">
[a-tag:사용자작성]    
[downlink:{asset_link}]
</div>

{summary}
'''

def render_media_card(title: str, display_url: str, download_url: str, summary: str, asset_url: str | None, jpg_thumb_url: str | None = None, webp_thumb_url: str | None = None, alpha_webp_url: str | None = None):
    container = st.container()
    cols = container.columns([1, 2])
    with cols[0]:
        st.image(display_url, caption="원본 이미지", use_container_width=True)
        if jpg_thumb_url:
            st.image(jpg_thumb_url, caption="썸네일 (JPG)", use_container_width=True)
        if webp_thumb_url:
            st.image(webp_thumb_url, caption="썸네일 (WebP)", use_container_width=True)
        if alpha_webp_url:
            st.image(alpha_webp_url, caption="썸네일 (Alpha WebP)", use_container_width=True)

    with cols[1]:
        st.markdown(f"### {title}")
        components.html(f"<button onclick=\"navigator.clipboard.writeText('{title}')\">파일명 복사</button>", height=50)        
        with st.expander("상세 정보"):
            st.markdown(f"**이미지 다운로드 링크:** {download_url}")
            st.markdown(f"**연관 자산 링크:** {asset_url}" if asset_url else ":warning: 연관 자산이 없습니다.")
            st.markdown(f"{summary}")
        snippet = generate_html_snippet(asset_url or download_url, summary)
        components.html(f"<textarea id='snippet_{title}' style='width:100%; height:800px;'>{snippet}</textarea><br><button onclick=\"navigator.clipboard.writeText(document.getElementById('snippet_{title}').value)\">스니펫 복사</button>", height=900)

# ========== MAIN ==========
def main():
    dbx = get_dropbox_client()
    st.set_page_config(page_title="Dropbox Asset Uploader", page_icon="📤")
    st.title("Dropbox Asset Uploader")
    st.markdown("이미지와 연관 자산 업로드, 진행사항을 텍스트로 제공합니다.")

    uploaded_files = st.file_uploader("파일 업로드", type=["jpg","jpeg","png","zip","sbsar"], accept_multiple_files=True)
    if not uploaded_files:
        st.session_state.pop("processed_files", None)
        st.session_state.pop("status_messages", None)
        return

    st.session_state.setdefault("processed_files", set())
    st.session_state.setdefault("status_messages", {})

    images = [f for f in uploaded_files if is_image_file(Path(f.name))]
    assets = {Path(f.name).stem: f for f in uploaded_files if f.name.lower().endswith((".zip",".sbsar"))}

    for img in images:
        stem = Path(img.name).stem
        if img.name in st.session_state["processed_files"]:
            continue

        st.session_state["status_messages"][img.name] = []
        status_placeholder = st.empty()

        def update_status(msg: str):
            st.session_state["status_messages"][img.name].append(f"{stem}: {msg}")
            status_placeholder.text_area(f"{stem} 진행상황", value="\n".join(st.session_state["status_messages"][img.name]), height=100)

        try:
            data = img.read()
            img_info = Image.open(io.BytesIO(data))
            width, height = img_info.size
            fmt = img_info.format
            update_status(f"업로드 준비 완료 (이미지: {width}px×{height}px, {fmt})")

            _, ext = split_filename(Path(img.name))
            path = resolve_unique_dropbox_path(dbx, f"/ae_assets/{stem}", ext)
            update_status("원본 파일 업로드 중...")
            upload_with_chunks(dbx, data, path)
            shared = get_or_create_shared_link(dbx, path)
            display_url = convert_dropbox_url(shared, 'raw=1')
            download_url = convert_dropbox_url(shared, 'dl=1')

            update_status("썸네일 생성 및 업로드 중...")
            thumb = img_info.copy()
            thumb.thumbnail((1000,1000))
            if thumb.mode in ("RGBA", "LA"):
                thumb = thumb.convert("RGB")
            buf = io.BytesIO()
            thumb.save(buf, format='JPEG', quality=80, optimize=True)
            thumb_bytes = buf.getvalue()
            thumb_path = resolve_unique_dropbox_path(dbx, f"/ae_assets/{stem}_thumb", 'jpg')
            upload_with_chunks(dbx, thumb_bytes, thumb_path)
            shared_thumb = get_or_create_shared_link(dbx, thumb_path)
            thumb_url = convert_dropbox_url(shared_thumb, 'raw=1')

            # WebP 썸네일
            buf_webp = io.BytesIO()
            thumb.save(buf_webp, format='WEBP', quality=80, method=6)
            thumb_webp_bytes = buf_webp.getvalue()
            thumb_webp_path = resolve_unique_dropbox_path(dbx, f"/ae_assets/{stem}_thumb", 'webp')
            upload_with_chunks(dbx, thumb_webp_bytes, thumb_webp_path)
            shared_thumb_webp = get_or_create_shared_link(dbx, thumb_webp_path)
            thumb_webp_url = convert_dropbox_url(shared_thumb_webp, 'raw=1')

            # 알파 WebP 썸네일
            update_status("알파 키잉 WebP 썸네일 생성 및 업로드 중...")
            alpha_removed = remove(data)  
            alpha_img = Image.open(io.BytesIO(alpha_removed)).convert("RGBA")
            alpha_thumb = alpha_img.copy()
            alpha_thumb.thumbnail((1000, 1000))
            buf_alpha_webp = io.BytesIO()
            alpha_thumb.save(buf_alpha_webp, format="WEBP", quality=90, method=6, lossless=True)
            alpha_webp_bytes = buf_alpha_webp.getvalue()
            alpha_webp_path = resolve_unique_dropbox_path(dbx, f"/ae_assets/{stem}_thumb_alpha", 'webp')
            upload_with_chunks(dbx, alpha_webp_bytes, alpha_webp_path)
            shared_alpha_webp = get_or_create_shared_link(dbx, alpha_webp_path)
            alpha_webp_url = convert_dropbox_url(shared_alpha_webp, 'raw=1')

            update_status("요약 생성 중 (GPT 자문)...")
            summary = generate_image_summary(thumb_url)

            asset_url = None
            if stem in assets:
                update_status("연관 자산 업로드 중...")
                f_asset = assets[stem]
                data_asset = f_asset.read()
                ext_asset = Path(f_asset.name).suffix.lstrip('.')
                asset_path = resolve_unique_dropbox_path(dbx, f"/ae_assets/{stem}", ext_asset)
                upload_with_chunks(dbx, data_asset, asset_path)
                shared_asset = get_or_create_shared_link(dbx, asset_path)
                asset_url = convert_dropbox_url(shared_asset, 'dl=1')

            update_status("완료")
            render_media_card(stem, display_url, download_url, summary, asset_url, thumb_url, thumb_webp_url, alpha_webp_url)
            st.session_state["processed_files"].add(img.name)
        except Exception as e:
            update_status(f"업로드 실패 - {e}")

# Streamlit 앱 실행
main()
