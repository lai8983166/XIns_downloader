import asyncio
import sys

# Windows: patchright 起浏览器需要 ProactorEventLoop（SelectorEventLoop 的 subprocess_exec
# 会抛 NotImplementedError）。uvicorn 0.49 的 --reload worker 是 spawn 子进程，其 loop 由
# asyncio_loop_factory(use_subprocess=True) 强制为 SelectorEventLoop，且 worker 不加载本项目
# 模块、无法 patch。故 Windows 下开发请勿加 --reload（无 --reload 时 uvicorn 用 ProactorEventLoop，
# 已验证 4 路由正常）。下方 set_event_loop_policy 仅作非 uvicorn 启动场景的防御。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import os
import re
import time
from http.cookies import SimpleCookie
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional, Literal
from urllib.parse import urlparse

import httpx
import instaloader
import instaloader.exceptions as instaloader_exceptions
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from collector.auto_job import AutoJobError, AutoJobManager
from collector.browser_session import session
from collector.config import settings as collector_settings
from collector.instagram_collector import (
    CollectorError,
    PostPreview,
    PostResource,
    ProfilePreview,
    preview_post,
    preview_profile,
    profile_post_resources,
)
from collector.quota import QuotaExceeded, scheduler
from collector.threads_collector import (
    preview_threads_profile,
    threads_post_resources,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # session 懒启动：未被采集路由使用前不拉起浏览器，未用不占资源
    yield
    await auto_manager.shutdown()
    await session.stop()


app = FastAPI(title="Instagram Preview Downloader MVP", lifespan=lifespan)

# 如果你的前端和后端端口不同，需要 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 本地开发先放开，正式项目再改成你的前端地址
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_ROOT = Path("downloads")
FRONTEND_DIST = Path("frontend/dist")
auto_manager = AutoJobManager(DOWNLOAD_ROOT)
ALLOWED_MEDIA_HOST_SUFFIXES = ("cdninstagram.com", "fbcdn.net")
PROFILE_DEFAULT_LIMIT = 6
PROFILE_MAX_LIMIT = 24
PROFILE_REQUEST_PAUSE_SECONDS = 2.0
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME")
INSTAGRAM_SESSION_FILE = os.getenv("INSTAGRAM_SESSION_FILE")
INSTAGRAM_SESSIONID = os.getenv("INSTAGRAM_SESSIONID")
INSTAGRAM_DS_USER_ID = os.getenv("INSTAGRAM_DS_USER_ID")
INSTAGRAM_CSRFTOKEN = os.getenv("INSTAGRAM_CSRFTOKEN")
INSTAGRAM_COOKIE_HEADER = os.getenv("INSTAGRAM_COOKIE_HEADER")
INSTAGRAM_USER_AGENT = os.getenv("INSTAGRAM_USER_AGENT")


class PreviewRequest(BaseModel):
    url: str


class MediaItem(BaseModel):
    index: int
    type: Literal["image", "video"]
    url: str
    thumbnail_url: Optional[str] = None
    filename: str


class PreviewResponse(BaseModel):
    shortcode: str
    owner_username: Optional[str] = None
    caption: Optional[str] = None
    resources: List[MediaItem]


class DownloadRequest(BaseModel):
    url: str
    selected_indices: Optional[List[int]] = None
    # selected_indices 为空或不传，默认下载全部


class DownloadResponse(BaseModel):
    status: str
    shortcode: str
    folder: str
    files: List[str]


class ProfilePreviewRequest(BaseModel):
    profile: str
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=PROFILE_DEFAULT_LIMIT, ge=1, le=PROFILE_MAX_LIMIT)


class ProfilePostItem(BaseModel):
    shortcode: str
    index: int
    url: str
    owner_username: Optional[str] = None
    caption: Optional[str] = None
    date_utc: str
    type: Literal["image", "video", "sidecar"]
    media_count: int
    thumbnail_url: str
    resources: List[MediaItem]


class ProfilePreviewResponse(BaseModel):
    username: str
    full_name: Optional[str] = None
    profile_pic_url: Optional[str] = None
    is_private: bool
    mediacount: int
    cursor: int
    next_cursor: Optional[int] = None
    has_more: bool
    posts: List[ProfilePostItem]


class ProfileDownloadItem(BaseModel):
    shortcode: str
    selected_indices: Optional[List[int]] = None


class ProfileDownloadRequest(BaseModel):
    username: str
    items: List[ProfileDownloadItem]


class ProfileDownloadResponse(BaseModel):
    status: str
    username: str
    folder: str
    files: List[str]


class AutoStartRequest(BaseModel):
    profile: str
    max_posts: Optional[int] = Field(default=None, gt=0)  # 本次最多处理新帖数；缺省不限


class AutoStartResponse(BaseModel):
    job_id: str
    username: str
    state: str


def _map_collector_error(exc: CollectorError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


def _acquire_manual_slot() -> None:
    """手动路径：立即执行但消耗调度器窗口名额（auto-download-mode design D4）。

    手动操作不进日程排队；满窗（名义时长未过）→ 429。
    """
    try:
        scheduler.acquire_now()
    except QuotaExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc))


def _resource_to_media_item(r: PostResource) -> MediaItem:
    return MediaItem(
        index=r.index,
        type=r.type,
        url=r.url,
        thumbnail_url=r.thumbnail_url,
        filename=r.filename,
    )


def _post_preview_to_response(data: PostPreview) -> PreviewResponse:
    return PreviewResponse(
        shortcode=data.shortcode,
        owner_username=data.owner_username,
        caption=data.caption,
        resources=[_resource_to_media_item(r) for r in data.resources],
    )


def _profile_preview_to_response(data: ProfilePreview) -> ProfilePreviewResponse:
    posts = [
        ProfilePostItem(
            shortcode=p.shortcode,
            index=p.index,
            url=p.url,
            owner_username=p.owner_username,
            caption=p.caption,
            date_utc=p.date_utc,
            type=p.type,
            media_count=p.media_count,
            thumbnail_url=p.thumbnail_url,
            resources=[_resource_to_media_item(r) for r in p.resources],
        )
        for p in data.posts
    ]
    return ProfilePreviewResponse(
        username=data.username,
        full_name=data.full_name,
        profile_pic_url=data.profile_pic_url,
        is_private=data.is_private,
        mediacount=data.mediacount,
        cursor=data.cursor,
        next_cursor=data.next_cursor,
        has_more=data.has_more,
        posts=posts,
    )


def extract_shortcode(url: str) -> str:
    """
    支持：
    https://www.instagram.com/p/DY6NvmAk5J5/
    https://www.instagram.com/p/DY6NvmAk5J5/?img_index=1
    https://www.instagram.com/reel/xxxx/
    https://www.instagram.com/tv/xxxx/
    """
    pattern = r"instagram\.com/(?:p|reel|tv)/([^/?#]+)/?"
    match = re.search(pattern, url)

    if not match:
        raise ValueError("无法识别 Instagram 链接，请使用 /p/、/reel/ 或 /tv/ 链接")

    return match.group(1)


def extract_profile_username(value: str) -> str:
    profile = value.strip()
    if not profile:
        raise ValueError("Profile username is required")

    if "instagram.com" in profile:
        match = re.search(r"instagram\.com/([^/?#]+)/?", profile)
        if not match:
            raise ValueError("Unable to read Instagram username from URL")
        profile = match.group(1)

    profile = profile.strip().lstrip("@")
    if profile in {"p", "reel", "tv", "stories", "explore"}:
        raise ValueError("Please enter a profile URL or username, not a post URL")
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", profile):
        raise ValueError("Invalid Instagram username")

    return profile


def create_loader(use_session: bool = True) -> instaloader.Instaloader:
    loader = instaloader.Instaloader(
        max_connection_attempts=1,
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        post_metadata_txt_pattern="",
    )

    if not use_session:
        return loader

    if INSTAGRAM_USER_AGENT:
        loader.context._session.headers.update({"User-Agent": INSTAGRAM_USER_AGENT})

    if INSTAGRAM_COOKIE_HEADER:
        cookie = SimpleCookie()
        cookie.load(INSTAGRAM_COOKIE_HEADER)
        for name, morsel in cookie.items():
            loader.context._session.cookies.set(name, morsel.value, domain=".instagram.com")
        if INSTAGRAM_USERNAME:
            loader.context.username = INSTAGRAM_USERNAME
    elif INSTAGRAM_SESSIONID:
        loader.context._session.cookies.set("sessionid", INSTAGRAM_SESSIONID, domain=".instagram.com")
        if INSTAGRAM_DS_USER_ID:
            loader.context._session.cookies.set("ds_user_id", INSTAGRAM_DS_USER_ID, domain=".instagram.com")
        if INSTAGRAM_CSRFTOKEN:
            loader.context._session.cookies.set("csrftoken", INSTAGRAM_CSRFTOKEN, domain=".instagram.com")
        if INSTAGRAM_USERNAME:
            loader.context.username = INSTAGRAM_USERNAME
    elif INSTAGRAM_USERNAME:
        loader.load_session_from_file(INSTAGRAM_USERNAME, INSTAGRAM_SESSION_FILE)

    return loader


def raise_instagram_http_error(error: Exception):
    message = str(error)
    lower_message = message.lower()

    if isinstance(error, instaloader_exceptions.TooManyRequestsException):
        raise HTTPException(
            status_code=429,
            detail="Instagram 临时限流了。请停止操作，等待 10-30 分钟后再试。",
        )

    if isinstance(
        error,
        (
            instaloader_exceptions.LoginRequiredException,
            instaloader_exceptions.BadCredentialsException,
            instaloader_exceptions.TwoFactorAuthRequiredException,
        ),
    ):
        raise HTTPException(status_code=401, detail="Instagram 需要登录或重新验证账号。")

    if isinstance(error, instaloader_exceptions.PrivateProfileNotFollowedException):
        raise HTTPException(status_code=403, detail="这是私密账号，当前会话没有访问权限。")

    if isinstance(error, instaloader_exceptions.ProfileNotExistsException):
        raise HTTPException(status_code=404, detail="找不到这个 Instagram 用户。")

    if isinstance(
        error,
        (
            instaloader_exceptions.QueryReturnedForbiddenException,
            instaloader_exceptions.QueryReturnedBadRequestException,
            instaloader_exceptions.BadResponseException,
            instaloader_exceptions.ConnectionException,
        ),
    ):
        if (
            "please wait a few minutes" in lower_message
            or "401 unauthorized" in lower_message
            or "json query to graphql/query" in lower_message
            or "expecting value" in lower_message
        ):
            raise HTTPException(
                status_code=429,
                detail="Instagram 没有返回正常 JSON，通常是登录态无效、账号需要验证或被临时限流。请检查 Cookie/Header 后重启后端，或等待 10-30 分钟再试。",
            )

        safe_message = message.replace("\n", " ")[:240]
        raise HTTPException(
            status_code=502,
            detail=f"Instagram 请求失败：{type(error).__name__}: {safe_message}",
        )

    raise HTTPException(
        status_code=500,
        detail=f"Instagram 请求失败：{type(error).__name__}: {message}",
    )


def get_post_from_url(url: str):
    shortcode = extract_shortcode(url)
    loader = create_loader(use_session=False)
    post = instaloader.Post.from_shortcode(loader.context, shortcode)
    return shortcode, post


def build_filename(shortcode: str, index: int, media_type: str, media_url: str) -> str:
    suffix = ".mp4" if media_type == "video" else ".jpg"

    # 尝试从 URL 里判断真实后缀
    clean_url = media_url.split("?")[0].lower()
    for ext in [".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"]:
        if clean_url.endswith(ext):
            suffix = ext
            break

    return f"{shortcode}_{index}{suffix}"


def preview_instagram_post(url: str) -> PreviewResponse:
    shortcode, post = get_post_from_url(url)

    resources: List[MediaItem] = []

    # 多图/多视频帖子
    if post.typename == "GraphSidecar":
        for idx, node in enumerate(post.get_sidecar_nodes(), start=1):
            if node.is_video:
                media_url = node.video_url
                media_type = "video"
                thumbnail_url = node.display_url
            else:
                media_url = node.display_url
                media_type = "image"
                thumbnail_url = node.display_url

            resources.append(
                MediaItem(
                    index=idx,
                    type=media_type,
                    url=media_url,
                    thumbnail_url=thumbnail_url,
                    filename=build_filename(shortcode, idx, media_type, media_url),
                )
            )

    # 单图/单视频帖子
    else:
        if post.is_video:
            media_url = post.video_url
            media_type = "video"
            thumbnail_url = post.url
        else:
            media_url = post.url
            media_type = "image"
            thumbnail_url = post.url

        resources.append(
            MediaItem(
                index=1,
                type=media_type,
                url=media_url,
                thumbnail_url=thumbnail_url,
                filename=build_filename(shortcode, 1, media_type, media_url),
            )
        )

    return PreviewResponse(
        shortcode=shortcode,
        owner_username=post.owner_username,
        caption=post.caption,
        resources=resources,
    )


def build_media_items_from_post(shortcode: str, post) -> List[MediaItem]:
    resources: List[MediaItem] = []

    if post.typename == "GraphSidecar":
        for idx, node in enumerate(post.get_sidecar_nodes(), start=1):
            if node.is_video:
                media_url = node.video_url
                media_type = "video"
                thumbnail_url = node.display_url
            else:
                media_url = node.display_url
                media_type = "image"
                thumbnail_url = node.display_url

            resources.append(
                MediaItem(
                    index=idx,
                    type=media_type,
                    url=media_url,
                    thumbnail_url=thumbnail_url,
                    filename=build_filename(shortcode, idx, media_type, media_url),
                )
            )
    else:
        if post.is_video:
            media_url = post.video_url
            media_type = "video"
            thumbnail_url = post.url
        else:
            media_url = post.url
            media_type = "image"
            thumbnail_url = post.url

        resources.append(
            MediaItem(
                index=1,
                type=media_type,
                url=media_url,
                thumbnail_url=thumbnail_url,
                filename=build_filename(shortcode, 1, media_type, media_url),
            )
        )

    return resources


def build_profile_post_item(post, index: int) -> ProfilePostItem:
    shortcode = post.shortcode
    resources = build_media_items_from_post(shortcode, post)
    post_type = "sidecar" if post.typename == "GraphSidecar" else ("video" if post.is_video else "image")

    return ProfilePostItem(
        shortcode=shortcode,
        index=index,
        url=f"https://www.instagram.com/p/{shortcode}/",
        owner_username=post.owner_username,
        caption=post.caption,
        date_utc=post.date_utc.isoformat(),
        type=post_type,
        media_count=len(resources),
        thumbnail_url=post.url,
        resources=resources,
    )


def preview_profile_posts(profile_value: str, cursor: int, limit: int) -> ProfilePreviewResponse:
    username = extract_profile_username(profile_value)
    bounded_limit = min(limit, PROFILE_MAX_LIMIT)
    loader = create_loader(use_session=True)
    profile = instaloader.Profile.from_username(loader.context, username)

    posts: List[ProfilePostItem] = []
    has_more = False
    stop_at = cursor + bounded_limit

    for index, post in enumerate(profile.get_posts()):
        if index < cursor:
            continue
        if index >= stop_at:
            has_more = True
            break
        posts.append(build_profile_post_item(post, index=index + 1))
        time.sleep(PROFILE_REQUEST_PAUSE_SECONDS)

    next_cursor = cursor + len(posts) if has_more else None
    return ProfilePreviewResponse(
        username=profile.username,
        full_name=profile.full_name,
        profile_pic_url=profile.profile_pic_url,
        is_private=profile.is_private,
        mediacount=profile.mediacount,
        cursor=cursor,
        next_cursor=next_cursor,
        has_more=has_more,
        posts=posts,
    )


async def download_file(file_url: str, output_path: Path):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.instagram.com/",
    }

    async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers=headers) as client:
        async with client.stream("GET", file_url) as response:
            response.raise_for_status()

            with open(output_path, "wb") as f:
                async for chunk in response.aiter_bytes():
                    if chunk:
                        f.write(chunk)


def validate_remote_media_url(media_url: str) -> str:
    parsed = urlparse(media_url)
    host = (parsed.hostname or "").lower()

    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Invalid media URL")

    if not any(host == suffix or host.endswith(f".{suffix}") for suffix in ALLOWED_MEDIA_HOST_SUFFIXES):
        raise HTTPException(status_code=400, detail="Unsupported media host")

    return media_url


@app.get("/")
def root():
    session_mode = (
        "cookie_header"
        if INSTAGRAM_COOKIE_HEADER
        else ("cookie" if INSTAGRAM_SESSIONID else ("session_file" if INSTAGRAM_USERNAME else "anonymous"))
    )
    return {
        "message": "Instagram Preview Downloader MVP is running",
        "preview": "POST /preview",
        "download": "POST /download",
        "profile_preview": "POST /profile/preview",
        "profile_download": "POST /profile/download",
        "instagram_session": session_mode,
    }


@app.post("/preview", response_model=PreviewResponse)
async def preview(req: PreviewRequest):
    if collector_settings.use_collector:
        try:
            shortcode = extract_shortcode(req.url)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        _acquire_manual_slot()
        try:
            return _post_preview_to_response(await preview_post(shortcode))
        except CollectorError as e:
            raise _map_collector_error(e)
        finally:
            scheduler.report_finish()

    # instaloader 应急兜底：仅当人工关闭 collector 且显式开启 fallback（仅单帖）
    if not collector_settings.enable_instaloader_fallback:
        raise HTTPException(status_code=503, detail="采集主路径已禁用，且 instaloader 兜底未开启")
    try:
        return preview_instagram_post(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except instaloader_exceptions.InstaloaderException as e:
        raise_instagram_http_error(e)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"预览失败：{type(e).__name__}: {e}")


@app.get("/media-proxy")
async def media_proxy(url: str = Query(...)):
    media_url = validate_remote_media_url(url)
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.instagram.com/",
    }

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers=headers) as client:
            response = await client.get(media_url)
            response.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail="Media request failed")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Media proxy failed: {type(e).__name__}")

    media_type = response.headers.get("content-type", "application/octet-stream").split(";")[0]
    return Response(
        content=response.content,
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=300",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.post("/profile/preview", response_model=ProfilePreviewResponse)
async def profile_preview(req: ProfilePreviewRequest):
    if not collector_settings.use_collector:
        raise HTTPException(status_code=503, detail="Profile 采集仅支持浏览器方案（collector），不提供 instaloader 兜底")
    _acquire_manual_slot()
    try:
        return _profile_preview_to_response(await preview_profile(req.profile, req.cursor, req.limit))
    except HTTPException:
        raise
    except CollectorError as e:
        raise _map_collector_error(e)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Profile preview failed: {type(e).__name__}: {e}")
    finally:
        scheduler.report_finish()


@app.post("/profile/download", response_model=ProfileDownloadResponse)
async def profile_download(req: ProfileDownloadRequest):
    try:
        username = extract_profile_username(req.username)
        if not req.items:
            raise HTTPException(status_code=400, detail="No posts selected")
        if not collector_settings.use_collector:
            raise HTTPException(status_code=503, detail="Profile 下载仅支持浏览器方案（collector），不提供 instaloader 兜底")

        _acquire_manual_slot()
        folder = DOWNLOAD_ROOT / f"profile_{username}"
        folder.mkdir(parents=True, exist_ok=True)
        saved_files = []

        for selected_post in req.items:
            shortcode = selected_post.shortcode.strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]+", shortcode):
                raise HTTPException(status_code=400, detail=f"Invalid shortcode: {shortcode}")
            resources = await profile_post_resources(username, shortcode, selected_post.selected_indices)
            for r in resources:
                output_path = folder / r.filename
                await download_file(r.url, output_path)
                saved_files.append(str(output_path).replace("\\", "/"))
        scheduler.report_finish()

        if not saved_files:
            raise HTTPException(status_code=400, detail="No selected media found")

        return ProfileDownloadResponse(
            status="done",
            username=username,
            folder=str(folder).replace("\\", "/"),
            files=saved_files,
        )

    except HTTPException:
        raise
    except CollectorError as e:
        raise _map_collector_error(e)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Profile download failed: {type(e).__name__}: {e}")


@app.post("/threads/profile/preview", response_model=ProfilePreviewResponse)
async def threads_profile_preview(req: ProfilePreviewRequest):
    if not collector_settings.use_collector:
        raise HTTPException(status_code=503, detail="Threads 采集仅支持浏览器方案（collector）")
    _acquire_manual_slot()
    try:
        return _profile_preview_to_response(await preview_threads_profile(req.profile, req.cursor, req.limit))
    except HTTPException:
        raise
    except CollectorError as e:
        raise _map_collector_error(e)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Threads profile preview failed: {type(e).__name__}: {e}")
    finally:
        scheduler.report_finish()


@app.post("/threads/profile/download", response_model=ProfileDownloadResponse)
async def threads_profile_download(req: ProfileDownloadRequest):
    try:
        username = req.username.strip().lstrip("@")
        if not req.items:
            raise HTTPException(status_code=400, detail="No posts selected")
        if not collector_settings.use_collector:
            raise HTTPException(status_code=503, detail="Threads 下载仅支持浏览器方案（collector）")

        _acquire_manual_slot()
        folder = DOWNLOAD_ROOT / f"threads_{username}"
        folder.mkdir(parents=True, exist_ok=True)
        saved_files = []

        for selected_post in req.items:
            code = selected_post.shortcode.strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]+", code):
                raise HTTPException(status_code=400, detail=f"Invalid code: {code}")
            resources = await threads_post_resources(username, code, selected_post.selected_indices)
            for r in resources:
                output_path = folder / r.filename
                await download_file(r.url, output_path)
                saved_files.append(str(output_path).replace("\\", "/"))
        scheduler.report_finish()

        if not saved_files:
            raise HTTPException(status_code=400, detail="No selected media found")

        return ProfileDownloadResponse(
            status="done",
            username=username,
            folder=str(folder).replace("\\", "/"),
            files=saved_files,
        )

    except HTTPException:
        raise
    except CollectorError as e:
        raise _map_collector_error(e)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Threads profile download failed: {type(e).__name__}: {e}")


@app.post("/download", response_model=DownloadResponse)
async def download(req: DownloadRequest):
    try:
        if collector_settings.use_collector:
            shortcode = extract_shortcode(req.url)
            _acquire_manual_slot()
            try:
                preview_data = _post_preview_to_response(await preview_post(shortcode))
            finally:
                scheduler.report_finish()
        elif collector_settings.enable_instaloader_fallback:
            preview_data = preview_instagram_post(req.url)
        else:
            raise HTTPException(status_code=503, detail="采集主路径已禁用，且 instaloader 兜底未开启")

        selected = req.selected_indices
        if selected:
            resources = [item for item in preview_data.resources if item.index in selected]
        else:
            resources = preview_data.resources

        if not resources:
            raise HTTPException(status_code=400, detail="没有选中的资源可下载")

        folder = DOWNLOAD_ROOT / f"post_{preview_data.shortcode}"
        folder.mkdir(parents=True, exist_ok=True)

        saved_files = []
        for item in resources:
            output_path = folder / item.filename
            await download_file(item.url, output_path)
            saved_files.append(str(output_path).replace("\\", "/"))

        return DownloadResponse(
            status="done",
            shortcode=preview_data.shortcode,
            folder=str(folder).replace("\\", "/"),
            files=saved_files,
        )

    except HTTPException:
        raise
    except CollectorError as e:
        raise _map_collector_error(e)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except instaloader_exceptions.InstaloaderException as e:
        raise_instagram_http_error(e)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"下载失败：{type(e).__name__}: {e}")


# ----------------------------- 自动模式（auto-download-mode） -----------------------------


@app.post("/profile/auto", response_model=AutoStartResponse, status_code=202)
async def profile_auto_start(req: AutoStartRequest):
    """启动自动任务：后台按调度器日程翻页采集并下载该用户全部内容。"""
    try:
        username = extract_profile_username(req.profile)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        job = await auto_manager.start(username, req.max_posts)
    except AutoJobError as e:
        if e.kind == "active_job":
            active = auto_manager.active_job()
            raise HTTPException(
                status_code=409,
                detail={"message": str(e), "job_id": active.job_id if active else None},
            )
        raise HTTPException(status_code=400, detail=str(e))  # manifest_corrupt 等
    return AutoStartResponse(job_id=job.job_id, username=username, state=job.state)


@app.get("/profile/auto/jobs/{job_id}")
async def profile_auto_status(job_id: str):
    """任务状态/进度（specs/auto-download Progress observability）。"""
    job = auto_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job 不存在（后端重启后注册表清空，可重新启动续跑）")
    return job.status_dict()


@app.post("/profile/auto/jobs/{job_id}/cancel")
async def profile_auto_cancel(job_id: str):
    """取消任务：置取消标志，当前动作与页内下载完成后退出；清单保留可增量续跑。"""
    job = auto_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job 不存在")
    if job.is_terminal:
        return {"status": job.state}
    job.request_cancel()
    return {"status": "cancelling"}


if FRONTEND_DIST.exists():
    app.mount("/app", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
