import os
import re
import sys
import requests
import datetime
import zipfile
import tempfile
import argparse
from functools import wraps
from concurrent.futures import ThreadPoolExecutor
from time import sleep
from base64 import b64decode
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from ebooklib import epub

# 配置信息
API_BASE = "https://qimao.tutublog.eu.org"  # 七猫小说API基础地址
AES_KEY = bytes.fromhex("32343263636238323330643730396531")  # AES解密密钥


def retry(max_retries=3, delay=2, exceptions=(Exception,)):
    """重试装饰器：在遇到指定异常时自动重试函数调用"""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            retries = 0
            while retries < max_retries:
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    retries += 1
                    sleep(delay)
            return func(*args, **kwargs)  # 最后一次尝试不捕获异常

        return wrapper

    return decorator


class QimaoAutoProcessor:
    """七猫小说自动化下载"""

    def __init__(self):
        # 请求头配置
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Referer": "https://www.qimao.com/",
        }
        self.novel_meta = {}  # 存储小说元数据
        self.temp_dir = tempfile.TemporaryDirectory()  # 创建临时目录
        self.novel_subdir = ""  # 小说专属输出目录
        self.book_id = ""  # 当前处理的小说ID
        self.catalog = {}  # 章节目录字典（chapter_id: title）
        self.failed_files = []  # 记录处理失败的文件

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """清理非法文件名字符，替换为下划线"""
        return re.sub(r'[\\/*?"<>|:]', "_", filename)

    def parse_book_id(self, input_str: str) -> str:
        """从用户输入中解析小说ID，支持直接输入ID或URL"""
        patterns = [
            r"www.qimao.com/shuku/(\d+)",  # 匹配URL格式
            r"^(\d+)$",  # 匹配纯数字ID
        ]
        for pattern in patterns:
            match = re.search(pattern, input_str)
            if match:
                return match.group(1)
        raise ValueError("无效输入，请输入正确的小说ID或链接")

    # 主运行流程 ------------------------------------------
    def run(self, user_input: str, output_dir: str = "output") -> None:
        """主运行流程"""
        try:
            # 直接输入模式
            self.book_id = self.parse_book_id(user_input)
            extract_dir = self.download_novel()
            self.get_metadata()

            # 创建小说专属目录：output/book_id/
            self.novel_subdir = os.path.join(output_dir, self.book_id)
            os.makedirs(self.novel_subdir, exist_ok=True)

            print(f"\n正在处理《{self.novel_meta['title']}》")
            self.get_chapters()
            self.process_files(extract_dir, self.novel_subdir)
        except Exception as e:
            print(f"\n处理失败：{str(e)}")
            sys.exit(1)
        finally:
            self.temp_dir.cleanup()  # 确保清理临时目录

    # 主功能 ----------------------------------------
    @retry(max_retries=3, exceptions=(requests.exceptions.RequestException,))
    def get_download_url(self) -> str:
        """获取小说压缩包的下载直链"""
        try:
            response = requests.get(
                f"{API_BASE}/{self.book_id}",
                headers=self.headers,
                allow_redirects=False,
                timeout=15,
            )
            if response.status_code == 302:
                return response.headers["Location"]
            raise RuntimeError(f"非预期状态码: {response.status_code}")
        except Exception as e:
            raise RuntimeError(f"下载链接获取失败: {str(e)}")

    def download_novel(self) -> str:
        """完整下载流程"""
        print("[1/5] 正在获取下载链接...")
        download_url = self.get_download_url()

        print("[2/5] 正在下载文件...")
        zip_path = os.path.join(self.temp_dir.name, f"{self.book_id}.zip")

        # 流式下载大文件
        response = requests.get(download_url, stream=True)
        with open(zip_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        print("[3/5] 正在解压文件...")
        extract_dir = os.path.join(self.temp_dir.name, "extracted")
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(extract_dir)

        return extract_dir

    @retry(max_retries=2)
    def get_metadata(self) -> None:
        """获取小说元数据"""
        try:
            response = requests.get(
                f"{API_BASE}/book/{self.book_id}/metadata",
                headers=self.headers,
                timeout=15,
            )
            data = response.json()["data"]
            self.novel_meta = {
                "title": data["title"],
                "author": data.get("author", "未知作者"),
                "image_link": data.get("image_link", ""),
                "big_image_link": data.get("big_image_link", ""),
                "tags": [tag["title"] for tag in data.get("book_tag_list", [])],
                "words_num": data.get("words_num", "未知字数"),
                "book_id": data["id"],
                "intro": data.get("intro", "暂无简介").replace("<br>", "\n"),
                "update_time": datetime.datetime.fromtimestamp(
                    int(data["update_time"])
                ).strftime("%Y-%m-%d %H:%M:%S"),
            }
        except Exception as e:
            self.novel_meta = {
                "title": "未知书名",
                "author": "未知作者",
                "image_link": "",
                "tags": [],
                "words_num": "未知",
                "book_id": self.book_id,
                "intro": "无简介",
                "update_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

    @retry(max_retries=3)
    def get_chapters(self) -> None:
        """获取章节目录信息"""
        try:
            response = requests.get(
                f"{API_BASE}/book/{self.book_id}/chapters",
                headers=self.headers,
                timeout=15,
            )
            chapters = response.json()["data"]["chapters"]
            self.catalog = {
                chap["id"]: chap["title"]
                for chap in sorted(chapters, key=lambda x: int(x["chapter_sort"]))
            }
        except Exception as e:
            raise RuntimeError(f"目录获取失败: {str(e)}")

    def _process_single_file(self, filename):
        """单文件处理流程"""
        try:
            chapter_id = os.path.splitext(filename)[0]
            original_path = os.path.join(self.extract_dir, filename)

            content = self.decrypt_file(original_path)
            chapter_title = self.catalog.get(chapter_id, f"未知章节_{chapter_id}")
            clean_title = self.sanitize_filename(chapter_title)

            output_path = os.path.join(self.novel_subdir, f"{clean_title}.txt")
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(content)

            return (chapter_id, chapter_title, output_path)
        except Exception as e:
            self.failed_files.append(filename)
            return None

    def decrypt_file(self, input_path: str) -> str:
        """AES解密文件内容"""
        with open(input_path, "r", encoding="utf-8") as f:
            encrypted = f.read()

        raw = b64decode(encrypted)
        iv, data = raw[:16], raw[16:]
        cipher = AES.new(AES_KEY, AES.MODE_CBC, iv=iv)
        return unpad(cipher.decrypt(data), AES.block_size).decode("utf-8")

    def process_files(self, extract_dir: str, output_dir: str) -> None:
        """批量处理解压后的文件"""
        self.novel_subdir = output_dir
        self.extract_dir = extract_dir
        os.makedirs(self.novel_subdir, exist_ok=True)
        decrypted_files = []

        file_list = sorted(
            [f for f in os.listdir(extract_dir) if f.endswith(".txt")],
            key=lambda x: int(x.split(".")[0]),
        )

        print("[4/5] 正在处理文件...")
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(self._process_single_file, f) for f in file_list]
            for future in futures:
                result = future.result()
                if result:
                    decrypted_files.append(result)

        if decrypted_files:
            merged_path = self.merge_files(
                sorted(decrypted_files, key=lambda x: int(x[0]))
            )
            self.generate_epub(decrypted_files, merged_path)
            self.clean_temp_files(merged_path)

        if self.failed_files:
            print(f"\n失败文件数: {len(self.failed_files)}")
            for f in self.failed_files[:3]:
                print(f"失败示例: {f}")

    def merge_files(self, files: list) -> str:
        """合并所有章节为单个txt文件"""
        meta_header = f"""书名：{self.novel_meta['title']}
作者：{self.novel_meta['author']}
标签：{'、'.join(self.novel_meta['tags'])}
字数：{self.novel_meta['words_num']}
书籍ID：{self.novel_meta['book_id']}
更新时间：{self.novel_meta['update_time']}

简介：
{self.novel_meta['intro']}

{"="*50}
"""
        novel_name = f"{self.sanitize_filename(self.novel_meta['title'])}.txt"
        output_path = os.path.join(self.novel_subdir, novel_name)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(meta_header)
            for _, title, path in files:
                with open(path, "r", encoding="utf-8") as cf:
                    f.write(f"\n\n{title}\n\n")
                    f.write(cf.read())

        return output_path

    def download_cover(self) -> bytes:
        """下载封面图片"""
        image_url = self.novel_meta.get("big_image_link") or self.novel_meta.get(
            "image_link"
        )
        if not image_url:
            return b""

        try:
            response = requests.get(image_url, timeout=10)
            response.raise_for_status()
            return response.content
        except Exception as e:
            print(f"封面下载失败: {str(e)}")
            return b""

    def generate_epub(self, files: list, merged_path: str) -> None:
        """生成EPUB电子书"""
        print("[5/5] 正在生成EPUB...")
        book = epub.EpubBook()
        book.set_title(self.novel_meta["title"])
        book.add_author(self.novel_meta["author"])
        book.set_language("zh-CN")

        style = """
        body { 
            font-family: "Microsoft YaHei", sans-serif; 
            line-height: 1.8;
            margin: 2em;
        }
        h1 { 
            font-size: 1.8em; 
            border-bottom: 1px solid #ccc;
            padding-bottom: 0.5em;
        }
        .content {
            white-space: pre-wrap;
            margin-top: 1.5em;
        }
        """
        nav_css = epub.EpubItem(
            uid="style_nav",
            file_name="style/nav.css",
            media_type="text/css",
            content=style,
        )
        book.add_item(nav_css)

        cover_data = self.download_cover()
        if cover_data:
            try:
                book.set_cover("cover.jpg", cover_data)
            except Exception as e:
                print(f"封面处理失败: {str(e)}")

        toc = []
        chapters = []

        intro_html = epub.EpubHtml(title="简介", file_name="intro.xhtml")
        intro_content = f"""
        <h1>{self.novel_meta['title']}</h1>
        {"<img src='cover.jpg' alt='封面' style='max-width: 80%;'/>" if cover_data else ""}
        <div class="content">{self.novel_meta['intro']}</div>
        """
        intro_html.content = intro_content
        book.add_item(intro_html)
        chapters.append(intro_html)

        future_data = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            for idx, (_, original_title, path) in enumerate(files, 1):
                future = executor.submit(
                    self._create_epub_chapter, original_title, path, idx, nav_css
                )
                future_data.append((future, original_title, idx))

            for future, chapter_title, chapter_idx in future_data:
                chapter = future.result()
                book.add_item(chapter)
                chapters.append(chapter)
                toc.append(
                    epub.Link(
                        href=chapter.file_name,
                        title=chapter_title,
                        uid=f"chap_{chapter_idx}",
                    )
                )

        book.toc = tuple(toc)
        book.add_item(epub.EpubNav())
        book.add_item(epub.EpubNcx())
        book.spine = ["nav", intro_html] + chapters

        epub_name = f"{self.sanitize_filename(self.novel_meta['title'])}.epub"
        epub_path = os.path.join(self.novel_subdir, epub_name)
        epub.write_epub(epub_path, book)
        print(f"EPUB生成完成：{epub_path}")

    def _create_epub_chapter(self, title, path, idx, css):
        """创建单个EPUB章节"""
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().replace("\n", "<br/>")

        chapter = epub.EpubHtml(
            title=title, file_name=f"chap_{idx}.xhtml", lang="zh-CN"
        )
        chapter.content = f"""
        <h1>{title}</h1>
        <div class="content">{content}</div>
        """
        chapter.add_item(css)
        return chapter

    def clean_temp_files(self, merged_path: str) -> None:
        """清理中间文件"""
        temp_files = [
            f
            for f in os.listdir(self.novel_subdir)
            if f.endswith(".txt") and os.path.join(self.novel_subdir, f) != merged_path
        ]

        print("正在清理临时文件...")
        for f in temp_files:
            try:
                os.remove(os.path.join(self.novel_subdir, f))
            except:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="七猫小说下载器")
    parser.add_argument("--input", required=True, help="小说ID或链接")
    parser.add_argument("--output", default="output", help="输出目录")

    args = parser.parse_args()

    processor = QimaoAutoProcessor()
    try:
        processor.run(args.input, args.output)
        print("\n处理完成！")
    except Exception as e:
        print(f"\n错误发生：{str(e)}")
        sys.exit(1)
