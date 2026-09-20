import { useEffect, useMemo, useRef, useState } from "react";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";
const PROFILE_PAGE_SIZE = 6;
const AUTO_POLL_INTERVAL_MS = 2000;
const AUTO_TERMINAL_STATES = ["done", "failed", "cancelled"];
const AUTO_STATE_LABELS = {
  starting: "启动中",
  running: "运行中",
  paused_cooldown: "冷却暂停",
  paused_signal_budget: "信号预算用尽",
  paused_circuit: "已熔断",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

function proxiedMediaUrl(mediaUrl) {
  if (!mediaUrl) {
    return "";
  }

  return `${API_BASE}/media-proxy?url=${encodeURIComponent(mediaUrl)}`;
}

function mediaKey(shortcode, index) {
  return `${shortcode}:${index}`;
}

function formatRemaining(iso) {
  const ms = new Date(iso).getTime() - Date.now();
  if (!Number.isFinite(ms) || ms <= 0) {
    return "即将恢复";
  }
  const minutes = Math.ceil(ms / 60000);
  return minutes >= 60 ? `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分后恢复` : `${minutes} 分钟后恢复`;
}

function formatClock(iso) {
  if (!iso) {
    return "";
  }
  return new Date(iso).toLocaleTimeString();
}

function App() {
  const [mode, setMode] = useState("post");
  const [platform, setPlatform] = useState("ig");
  const [input, setInput] = useState("");
  const [postPreview, setPostPreview] = useState(null);
  const [postSelected, setPostSelected] = useState(() => new Set());
  const [profilePreview, setProfilePreview] = useState(null);
  const [profileSelected, setProfileSelected] = useState(() => new Set());
  const [profileStartOffset, setProfileStartOffset] = useState("");
  const [autoMaxPosts, setAutoMaxPosts] = useState("");
  const [autoJobId, setAutoJobId] = useState(null);
  const [autoStatus, setAutoStatus] = useState(null);
  const [autoStarting, setAutoStarting] = useState(false);
  const [lightbox, setLightbox] = useState(null);
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [downloading, setDownloading] = useState(false);

  const autoTerminal = AUTO_TERMINAL_STATES.includes(autoStatus?.state);

  const isThreads = platform === "threads";
  const profilePath = isThreads ? "/threads/profile/preview" : "/profile/preview";
  const profileDownloadPath = isThreads ? "/threads/profile/download" : "/profile/download";
  // Threads 只有 profile；IG 有 post/profile。effectiveMode 统一判断
  const effectiveMode = isThreads ? "profile" : mode;

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

  const selectedCount = effectiveMode === "post" ? postSelected.size : profileSelected.size;

  // 自动任务状态轮询：job 存在且未终态时每 2s 拉一次
  useEffect(() => {
    if (!autoJobId || autoTerminal) {
      return undefined;
    }
    let cancelled = false;
    async function pollAutoJob() {
      try {
        const response = await fetch(`${API_BASE}/profile/auto/jobs/${autoJobId}`);
        if (response.status === 404) {
          setAutoStatus(null);
          setStatus("任务不存在（后端重启后注册表清空），可重新启动续跑");
          setAutoJobId(null);
          return;
        }
        const data = await response.json().catch(() => ({}));
        if (!cancelled && response.ok) {
          setAutoStatus(data);
        }
      } catch {
        // 网络抖动：下一轮再试
      }
    }
    pollAutoJob();
    const timer = setInterval(pollAutoJob, AUTO_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [autoJobId, autoTerminal]);

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

  function resetResults(nextMode = effectiveMode) {
    setPostPreview(null);
    setProfilePreview(null);
    setPostSelected(new Set());
    setProfileSelected(new Set());
    setLightbox(null);
    setStatus(
      nextMode === "profile"
        ? "输入用户名或主页链接后分页预览。"
        : nextMode === "auto"
          ? "输入 Instagram 用户主页链接，启动后自动翻页下载全部内容。"
          : "输入帖子链接后预览图片。"
    );
  }

  function handleModeChange(nextMode) {
    setMode(nextMode);
    resetResults(nextMode);
  }

  function handlePlatformChange(nextPlatform) {
    setPlatform(nextPlatform);
    if (nextPlatform === "threads") {
      setMode("profile");
    }
    setPostPreview(null);
    setProfilePreview(null);
    setPostSelected(new Set());
    setProfileSelected(new Set());
    setLightbox(null);
    setStatus(nextPlatform === "threads" ? "输入 Threads 用户名后分页预览。" : "输入帖子链接后预览图片。");
  }

  async function handleSubmit(event) {
    event.preventDefault();
    if (effectiveMode === "profile") {
      await loadProfilePage({ reset: true });
      return;
    }
    if (effectiveMode === "auto") {
      await startAutoJob();
      return;
    }
    await loadPostPreview();
  }

  async function startAutoJob() {
    const profile = input.trim();
    if (!profile) {
      setStatus("请输入 Instagram 用户名或主页链接");
      return;
    }
    setAutoStarting(true);
    try {
      const response = await fetch(`${API_BASE}/profile/auto`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          profile,
          max_posts: autoMaxPosts ? Number(autoMaxPosts) : null,
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (response.status === 409 && data.detail?.job_id) {
        setStatus(data.detail.message || "已有活跃任务");
        setAutoJobId(data.detail.job_id); // 跳转到现有任务视图
        return;
      }
      if (!response.ok) {
        setStatus(typeof data.detail === "string" ? data.detail : data.detail?.message || "启动失败，请稍后重试");
        return;
      }
      setAutoStatus(null);
      setAutoJobId(data.job_id);
      setStatus(`自动任务已启动：@${data.username}，将按拟人日程自动翻页下载`);
    } catch (error) {
      setStatus(`启动失败：${error.message}`);
    } finally {
      setAutoStarting(false);
    }
  }

  async function cancelAutoJob() {
    if (!autoJobId) {
      return;
    }
    try {
      await fetch(`${API_BASE}/profile/auto/jobs/${autoJobId}/cancel`, { method: "POST" });
      setStatus("已请求取消，任务将在当前动作完成后停止（清单保留，可再次启动续跑）");
    } catch (error) {
      setStatus(`取消失败：${error.message}`);
    }
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
      setStatus(isThreads ? "请输入 Threads 用户名或主页链接" : "请输入 Instagram 用户名或主页链接");
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
      const data = await requestJson(profilePath, {
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
    if (effectiveMode === "profile") {
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
      const data = await requestJson(profileDownloadPath, {
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
    (effectiveMode === "post" && postResources.length > 0 && postSelected.size === 0) ||
    (effectiveMode === "profile" && profileMedia.length > 0 && profileSelected.size === 0);

  return (
    <main className="page">
      <header className="topbar">
        <form className="urlForm" onSubmit={handleSubmit}>
          <div className="modeSwitch" role="tablist" aria-label="平台">
            <button
              className={platform === "ig" ? "modeButton active" : "modeButton"}
              type="button"
              onClick={() => handlePlatformChange("ig")}
            >
              Instagram
            </button>
            <button
              className={platform === "threads" ? "modeButton active" : "modeButton"}
              type="button"
              onClick={() => handlePlatformChange("threads")}
            >
              Threads
            </button>
          </div>
          {!isThreads && (
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
              <button
                className={mode === "auto" ? "modeButton active" : "modeButton"}
                type="button"
                onClick={() => handleModeChange("auto")}
              >
                自动
              </button>
            </div>
          )}
          <input
            className="urlInput"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder={
              isThreads
                ? "输入 Threads 用户名或主页链接"
                : effectiveMode === "post"
                  ? "粘贴 Instagram 帖子、Reel 或 TV 链接"
                  : "输入用户名或主页链接"
            }
            aria-label={
              isThreads ? "Threads 用户名" : effectiveMode === "post" ? "Instagram 链接" : "Instagram 用户名"
            }
          />
          {effectiveMode === "profile" && (
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
          {effectiveMode === "auto" && (
            <input
              className="urlInput"
              type="number"
              min="1"
              value={autoMaxPosts}
              onChange={(event) => setAutoMaxPosts(event.target.value)}
              placeholder="本次最多新帖数（留空=全部）"
              aria-label="自动模式上限"
              style={{ maxWidth: 200 }}
            />
          )}
          <button className="primaryButton" disabled={loading || autoStarting} type="submit">
            {effectiveMode === "auto" ? (autoStarting ? "启动中" : "启动") : loading ? "获取中" : "确认"}
          </button>
          {effectiveMode !== "auto" && (
            <button className="downloadButton" disabled={canDownload} type="button" onClick={handleDownload}>
              {downloading ? "下载中" : "下载"}
            </button>
          )}
        </form>
      </header>

      <section className="content">
        <div className="summary">
          <div>
            <h1>XIns Downloader</h1>
            <p>{status || "输入链接后预览图片，勾选需要的资源再下载。"}</p>
          </div>
          <div className="meta">
            {effectiveMode === "post" && postPreview && <span>{postPreview.shortcode}</span>}
            {effectiveMode === "profile" && profilePreview && (
              <>
                <span>@{profilePreview.username}</span>
                <span>{profilePosts.length}/{profilePreview.mediacount} 帖子</span>
              </>
            )}
            {(postResources.length > 0 || profileMedia.length > 0) && <span>{selectedCount} 已选</span>}
          </div>
        </div>

        {effectiveMode === "profile" && profilePreview && (
          <div className="profileBar">
            <img src={proxiedMediaUrl(profilePreview.profile_pic_url)} alt={profilePreview.username} />
            <div>
              <strong>{profilePreview.full_name || profilePreview.username}</strong>
              <span>{profilePreview.is_private ? "私密账号：仅能预览你有权限访问的内容" : "公开账号"}</span>
            </div>
          </div>
        )}

        {effectiveMode === "auto" ? (
          <AutoJobPanel jobId={autoJobId} status={autoStatus} onCancel={cancelAutoJob} />
        ) : effectiveMode === "post" ? (
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

function AutoJobPanel({ jobId, status, onCancel }) {
  if (!jobId) {
    return (
      <div className="emptyState">
        <div className="emptyMark">自动</div>
        <p>输入主页链接并点击「启动」，后台将按拟人日程自动翻页下载全部内容</p>
      </div>
    );
  }

  const state = status?.state ?? "starting";
  const stateLabel = AUTO_STATE_LABELS[state] ?? state;
  const counts = status?.counts ?? {};
  const total = status?.mediacount ?? null;
  const processed = status?.posts_total ?? 0;
  const progressPercent =
    total && total > 0 ? Math.min(100, Math.round((processed / total) * 100)) : null;
  const terminal = AUTO_TERMINAL_STATES.includes(state);

  return (
    <div className="autoPanel">
      <div className="autoHeader">
        <span className={`stateChip ${terminal ? "stateTerminal" : state.startsWith("paused") ? "statePaused" : "stateActive"}`}>
          {stateLabel}
        </span>
        <strong>@{status?.username ?? "…"}</strong>
        {status?.max_posts ? <span>上限 {status.max_posts} 新帖</span> : null}
        <span>job {jobId}</span>
      </div>

      {status?.paused_reason && (
        <p className="autoReason">
          {status.paused_reason}
          {status.resume_at ? `（${formatRemaining(status.resume_at)}，${formatClock(status.resume_at)}）` : ""}
        </p>
      )}

      <div className="autoProgress">
        <div className="autoProgressLabel">
          <span>
            已处理 {processed}/{total ?? "?"} 帖
            {status?.has_more === false ? "（已到时间线末尾）" : ""}
          </span>
          {progressPercent !== null && <span>{progressPercent}%</span>}
        </div>
        <div className="autoProgressBar">
          <div className="autoProgressFill" style={{ width: `${progressPercent ?? 100}%` }} />
        </div>
      </div>

      <div className="autoCounts">
        <span>已下载 {counts.downloaded ?? 0}</span>
        <span>跳过 {counts.skipped ?? 0}</span>
        <span>失败 {counts.failed ?? 0}</span>
        {status?.next_action_at && <span>下一动作 {formatClock(status.next_action_at)}</span>}
      </div>

      {status?.recent_failures?.length > 0 && (
        <details className="autoFailures">
          <summary>最近失败（{status.recent_failures.length}）</summary>
          <ul>
            {status.recent_failures.slice(-5).reverse().map((failure, index) => (
              <li key={index}>
                <code>{failure.shortcode ?? "-"}</code> {failure.detail}
              </li>
            ))}
          </ul>
        </details>
      )}

      <div className="autoActions">
        {!terminal ? (
          <button className="secondaryButton" type="button" onClick={onCancel}>
            取消任务
          </button>
        ) : (
          <span className="autoDoneHint">
            {state === "done" ? "本次采集完成；再次启动可增量拉取新帖" : `任务已${stateLabel}；清单已保留，可再次启动续跑`}
          </span>
        )}
      </div>
    </div>
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
