import { useMemo, useState } from "react";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";
const PROFILE_PAGE_SIZE = 6;

function proxiedMediaUrl(mediaUrl) {
  if (!mediaUrl) {
    return "";
  }

  return `${API_BASE}/media-proxy?url=${encodeURIComponent(mediaUrl)}`;
}

function mediaKey(shortcode, index) {
  return `${shortcode}:${index}`;
}

function App() {
  const [mode, setMode] = useState("post");
  const [input, setInput] = useState("");
  const [postPreview, setPostPreview] = useState(null);
  const [postSelected, setPostSelected] = useState(() => new Set());
  const [profilePreview, setProfilePreview] = useState(null);
  const [profileSelected, setProfileSelected] = useState(() => new Set());
  const [profileStartOffset, setProfileStartOffset] = useState("");
  const [lightbox, setLightbox] = useState(null);
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [downloading, setDownloading] = useState(false);

  const postResources = postPreview?.resources ?? [];
  const profilePosts = profilePreview?.posts ?? [];
  const profileMedia = useMemo(
    () =>
      profilePosts.flatMap((post) =>
        post.resources.map((item) => ({
          ...item,
          shortcode: post.shortcode,
          postUrl: post.url,
          caption: post.caption,
          dateUtc: post.date_utc,
        }))
      ),
    [profilePosts]
  );

  const selectedCount = mode === "post" ? postSelected.size : profileSelected.size;

  async function requestJson(path, body) {
    const response = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));

    if (!response.ok) {
      throw new Error(data.detail || "请求失败，请稍后重试");
    }

    return data;
  }

  function resetResults(nextMode = mode) {
    setPostPreview(null);
    setProfilePreview(null);
    setPostSelected(new Set());
    setProfileSelected(new Set());
    setLightbox(null);
    setStatus(nextMode === "profile" ? "输入用户名或主页链接后分页预览。" : "输入帖子链接后预览图片。");
  }

  function handleModeChange(nextMode) {
    setMode(nextMode);
    resetResults(nextMode);
  }

  async function handleSubmit(event) {
    event.preventDefault();
    if (mode === "profile") {
      await loadProfilePage({ reset: true });
      return;
    }
    await loadPostPreview();
  }

  async function loadPostPreview() {
    const nextUrl = input.trim();
    if (!nextUrl) {
      setStatus("请输入 Instagram 帖子链接");
      return;
    }

    setLoading(true);
    setStatus("正在获取预览...");
    setPostPreview(null);
    setPostSelected(new Set());

    try {
      const data = await requestJson("/preview", { url: nextUrl });
      setPostPreview(data);
      setPostSelected(new Set(data.resources.map((item) => item.index)));
      setStatus(`已找到 ${data.resources.length} 个资源`);
    } catch (error) {
      setStatus(error.message);
    } finally {
      setLoading(false);
    }
  }

  async function loadProfilePage({ reset }) {
    const profile = input.trim();
    if (!profile) {
      setStatus("请输入 Instagram 用户名或主页链接");
      return;
    }

    const startOffset = Number(profileStartOffset) || 0;
    const cursor = reset ? startOffset : profilePreview?.next_cursor;
    if (cursor === null || cursor === undefined) {
      return;
    }

    if (reset) {
      setLoading(true);
      setProfilePreview(null);
      setProfileSelected(new Set());
      setStatus(startOffset > 0 ? `正在从第 ${startOffset + 1} 个加载...` : "正在获取 Profile 首屏内容...");
    } else {
      setLoadingMore(true);
      setStatus("正在加载下一页...");
    }

    try {
      const data = await requestJson("/profile/preview", {
        profile,
        cursor,
        limit: PROFILE_PAGE_SIZE,
      });

      // 分页替换显示：每次只显示当前这批（不累积）；加载更多清空旧勾选
      setProfilePreview({ ...data, posts: data.posts });
      if (!reset) {
        setProfileSelected(new Set());
      }

      setStatus(`显示第 ${data.cursor + 1}–${data.cursor + data.posts.length} 个，共 ${data.mediacount ?? "?"}（本页 ${data.posts.length} 个）`);
    } catch (error) {
      setStatus(error.message);
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  }

  async function handleDownload() {
    if (mode === "profile") {
      await downloadProfileSelection();
      return;
    }
    await downloadPostSelection();
  }

  async function downloadPostSelection() {
    if (!input.trim()) {
      setStatus("请输入 Instagram 帖子链接");
      return;
    }

    if (postResources.length > 0 && postSelected.size === 0) {
      setStatus("请先勾选要下载的图片");
      return;
    }

    setDownloading(true);
    setStatus("正在下载已勾选资源...");

    try {
      const data = await requestJson("/download", {
        url: input.trim(),
        selected_indices: Array.from(postSelected).sort((a, b) => a - b),
      });
      setStatus(`下载完成：${data.files.length} 个文件已保存到 ${data.folder}`);
    } catch (error) {
      setStatus(error.message);
    } finally {
      setDownloading(false);
    }
  }

  async function downloadProfileSelection() {
    if (!profilePreview) {
      setStatus("请先预览 Profile");
      return;
    }

    if (profileSelected.size === 0) {
      setStatus("请先勾选要下载的图片");
      return;
    }

    const itemsByPost = new Map();
    profileMedia.forEach((item) => {
      const key = mediaKey(item.shortcode, item.index);
      if (!profileSelected.has(key)) {
        return;
      }

      const indices = itemsByPost.get(item.shortcode) ?? [];
      indices.push(item.index);
      itemsByPost.set(item.shortcode, indices);
    });

    setDownloading(true);
    setStatus("正在下载已勾选的 Profile 资源...");

    try {
      const data = await requestJson("/profile/download", {
        username: profilePreview.username,
        items: Array.from(itemsByPost, ([shortcode, selected_indices]) => ({
          shortcode,
          selected_indices,
        })),
      });
      setProfileSelected(new Set());
      setStatus(`下载完成：${data.files.length} 个文件已保存到 ${data.folder}`);
    } catch (error) {
      setStatus(error.message);
    } finally {
      setDownloading(false);
    }
  }

  function togglePostSelected(index) {
    setPostSelected((current) => {
      const next = new Set(current);
      if (next.has(index)) {
        next.delete(index);
      } else {
        next.add(index);
      }
      return next;
    });
  }

  function toggleProfileSelected(shortcode, index) {
    const key = mediaKey(shortcode, index);
    setProfileSelected((current) => {
      const next = new Set(current);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  }

  const canDownload =
    downloading ||
    (mode === "post" && postResources.length > 0 && postSelected.size === 0) ||
    (mode === "profile" && profileMedia.length > 0 && profileSelected.size === 0);

  return (
    <main className="page">
      <header className="topbar">
        <form className="urlForm" onSubmit={handleSubmit}>
          <div className="modeSwitch" role="tablist" aria-label="下载模式">
            <button
              className={mode === "post" ? "modeButton active" : "modeButton"}
              type="button"
              onClick={() => handleModeChange("post")}
            >
              单帖
            </button>
            <button
              className={mode === "profile" ? "modeButton active" : "modeButton"}
              type="button"
              onClick={() => handleModeChange("profile")}
            >
              Profile
            </button>
          </div>
          <input
            className="urlInput"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder={mode === "profile" ? "输入用户名或主页链接" : "粘贴 Instagram 帖子、Reel 或 TV 链接"}
            aria-label={mode === "profile" ? "Instagram 用户名" : "Instagram 链接"}
          />
          {mode === "profile" && (
            <input
              className="urlInput"
              type="number"
              min="0"
              value={profileStartOffset}
              onChange={(event) => setProfileStartOffset(event.target.value)}
              placeholder="从第几个开始（留空=最新）"
              aria-label="起始位置（跳过前 N 个）"
              style={{ maxWidth: 180 }}
            />
          )}
          <button className="primaryButton" disabled={loading} type="submit">
            {loading ? "获取中" : "确认"}
          </button>
          <button className="downloadButton" disabled={canDownload} type="button" onClick={handleDownload}>
            {downloading ? "下载中" : "下载"}
          </button>
        </form>
      </header>

      <section className="content">
        <div className="summary">
          <div>
            <h1>XIns Downloader</h1>
            <p>{status || "输入链接后预览图片，勾选需要的资源再下载。"}</p>
          </div>
          <div className="meta">
            {mode === "post" && postPreview && <span>{postPreview.shortcode}</span>}
            {mode === "profile" && profilePreview && (
              <>
                <span>@{profilePreview.username}</span>
                <span>{profilePosts.length}/{profilePreview.mediacount} 帖子</span>
              </>
            )}
            {(postResources.length > 0 || profileMedia.length > 0) && <span>{selectedCount} 已选</span>}
          </div>
        </div>

        {mode === "profile" && profilePreview && (
          <div className="profileBar">
            <img src={proxiedMediaUrl(profilePreview.profile_pic_url)} alt={profilePreview.username} />
            <div>
              <strong>{profilePreview.full_name || profilePreview.username}</strong>
              <span>{profilePreview.is_private ? "私密账号：仅能预览你有权限访问的内容" : "公开账号"}</span>
            </div>
          </div>
        )}

        {mode === "post" ? (
          <MediaGrid
            items={postResources.map((item) => ({
              ...item,
              checked: postSelected.has(item.index),
              imageUrl: proxiedMediaUrl(item.thumbnail_url || item.url),
              label: item.filename,
              onToggle: () => togglePostSelected(item.index),
            }))}
            onZoom={setLightbox}
          />
        ) : (
          <>
            <MediaGrid
              items={profileMedia.map((item) => ({
                ...item,
                checked: profileSelected.has(mediaKey(item.shortcode, item.index)),
                imageUrl: proxiedMediaUrl(item.thumbnail_url || item.url),
                label: `${item.shortcode} / ${item.filename}`,
                badge: item.shortcode,
                onToggle: () => toggleProfileSelected(item.shortcode, item.index),
              }))}
              onZoom={setLightbox}
            />
            {profilePreview?.has_more && (
              <div className="loadMoreRow">
                <button className="secondaryButton" type="button" disabled={loadingMore} onClick={() => loadProfilePage({ reset: false })}>
                  {loadingMore ? "加载中" : "加载更多"}
                </button>
              </div>
            )}
          </>
        )}
      </section>

      {lightbox && (
        <div className="lightbox" role="dialog" aria-modal="true" onClick={() => setLightbox(null)}>
          <button className="closeButton" type="button" aria-label="关闭预览">
            x
          </button>
          <img src={lightbox.imageUrl} alt={lightbox.filename} onClick={(event) => event.stopPropagation()} />
        </div>
      )}
    </main>
  );
}

function MediaGrid({ items, onZoom }) {
  if (items.length === 0) {
    return (
      <div className="emptyState">
        <div className="emptyMark">IG</div>
        <p>等待预览内容</p>
      </div>
    );
  }

  return (
    <div className="mediaGrid">
      {items.map((item) => (
        <article className="mediaCard" key={`${item.shortcode || "post"}-${item.index}-${item.filename}`}>
          <div className="imageWrap">
            <img src={item.imageUrl} alt={item.filename} loading="lazy" />
            <button
              className="zoomButton"
              type="button"
              onClick={() => onZoom(item)}
              aria-label="放大图片"
              title="放大图片"
            >
              +
            </button>
            {item.type === "video" && <span className="typeBadge">VIDEO</span>}
            {item.badge && <span className="postBadge">{item.badge}</span>}
          </div>
          <label className="checkRow">
            <input checked={item.checked} type="checkbox" onChange={item.onToggle} />
            <span>{item.label}</span>
          </label>
        </article>
      ))}
    </div>
  );
}

export default App;
