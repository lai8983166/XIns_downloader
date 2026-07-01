import os
import re
import sys
import instaloader


def extract_shortcode(url: str) -> str:
    """
    支持：
    https://www.instagram.com/p/DY6NvmAk5J5/
    https://www.instagram.com/p/DY6NvmAk5J5/?img_index=1
    https://www.instagram.com/reel/xxxx/
    """
    pattern = r"instagram\.com/(?:p|reel|tv)/([^/?#]+)/?"
    match = re.search(pattern, url)

    if not match:
        raise ValueError("无法从链接中识别 Instagram shortcode，请确认是 /p/、/reel/ 或 /tv/ 链接")

    return match.group(1)


def download_instagram_post(url: str, output_root: str = "downloads"):
    shortcode = extract_shortcode(url)

    os.makedirs(output_root, exist_ok=True)

    loader = instaloader.Instaloader(
        dirname_pattern=os.path.join(output_root, "{target}"),
        filename_pattern="{shortcode}_{date_utc}_UTC",
        download_pictures=True,
        download_videos=True,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=True,
        compress_json=False,
        post_metadata_txt_pattern=""
    )

    print(f"识别 shortcode: {shortcode}")
    print("正在获取帖子信息...")

    post = instaloader.Post.from_shortcode(loader.context, shortcode)

    target = f"post_{shortcode}"

    print(f"开始下载到: {os.path.join(output_root, target)}")
    loader.download_post(post, target=target)

    print("下载完成")


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        input_url = sys.argv[1]
    else:
        input_url = input("输入 Instagram 链接：").strip()

    download_instagram_post(input_url)