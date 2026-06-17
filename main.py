"""
JMComic 禁漫搜索 - AstrBot 插件
/jm 关键词    → 分页搜索（8条/页），合并转发展示封面+名字
/jm <序号>    → 有缓存：全局序号看详情；无缓存：禁漫ID查详情
/jm看图 N     → 全局第N本 → 下载第1话 → 生成PDF发送
+/下一页      → 下一页
-/上一页      → 上一页
"""
import os
import glob
import shutil
import base64
import uuid
import tempfile
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from astrbot.api.message_components import Image, Nodes, Plain, Node, File
from astrbot.api import logger
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter

try:
    import jmcomic
    from jmcomic import (
        JmModuleConfig,
        JmOption,
        JmMagicConstants,
        JmAlbumDetail,
        JmSearchPage,
    )
    HAS_JMCOMIC = True
except ImportError:
    HAS_JMCOMIC = False

PAGE_SIZE = 8
MAX_PAGES = 3
MAX_RESULTS = PAGE_SIZE * MAX_PAGES  # 24
CACHE_TTL = 300  # 搜索缓存 5 分钟
COVER_MAX_EDGE = 400  # 封面长边限制（减少 base64 体积，避免 retcode=1200）

_thread_pool = ThreadPoolExecutor(max_workers=3)


def _file_to_base64_image(filepath: str, max_edge: int = 0, quality: int = 0) -> str:
    """读取图片文件并返回 base64 data URL。可选压缩参数。"""
    if max_edge > 0 and quality > 0:
        try:
            from PIL import Image as PILImage
            img = PILImage.open(filepath)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            w, h = img.size
            if max(w, h) > max_edge:
                ratio = max_edge / max(w, h)
                img = img.resize((int(w * ratio), int(h * ratio)), PILImage.LANCZOS)
            img = img.convert("RGB")
            import io
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality, optimize=True)
            img.close()
            data = base64.b64encode(buf.getvalue()).decode("ascii")
            mime = "image/jpeg"
            return f"base64://{data}"
        except Exception:
            pass

    with open(filepath, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    ext = os.path.splitext(filepath)[1].lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}
    mime = mime_map.get(ext, "image/jpeg")
    return f"base64://{data}"


@register(
    "astrbot_plugin_jmcomic",
    "JMComic禁漫搜索",
    "禁漫本子分页搜索/查看详情/看图PDF",
    "2.2.0",
)
class JMComicPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._load_config()
        self._client = None
        self._option = None
        # user_id → {"results": [...], "page": 0, "total_pages": 1, "keyword": "少女", "expires_at": timestamp}
        self._search_cache = {}
        # user_id → JmAlbumDetail (上次查看详情选中的本子)
        self._photo_cache = {}
        # album_id → base64url 封面缓存
        self._cover_cache = {}
        self.temp_dir = os.path.join(tempfile.gettempdir(), "astrbot_jmcomic")
        os.makedirs(self.temp_dir, exist_ok=True)

        if not HAS_JMCOMIC:
            logger.warning("jmcomic 库未安装，插件功能不可用。请执行 pip install jmcomic")
        else:
            self._init_jm_client()

    # ============ 缓存过期辅助 ============
    def _cache_valid(self, user_id: str) -> bool:
        """检查搜索缓存是否存在且未过期"""
        cache = self._search_cache.get(user_id)
        if not cache:
            return False
        if time.time() > cache.get("expires_at", 0):
            del self._search_cache[user_id]
            return False
        return True

    # ============ 配置加载 ============
    def _load_config(self):
        astr_cfg = getattr(self, "config", {}) or {}
        self._client_impl = astr_cfg.get("client_impl", "api")
        self._proxy = astr_cfg.get("proxy", "")
        self._domains = astr_cfg.get("domains", [])

        config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
        if os.path.exists(config_path):
            try:
                import yaml
                with open(config_path, "r", encoding="utf-8") as f:
                    file_cfg = yaml.safe_load(f) or {}
                self._client_impl = self._client_impl or file_cfg.get("client_impl", "api")
                self._proxy = self._proxy or file_cfg.get("proxy", "")
                self._domains = self._domains or file_cfg.get("domains", [])
            except ImportError:
                pass

    def _init_jm_client(self):
        try:
            JmModuleConfig.FLAG_ENABLE_JM_LOG = False
            option_dict = {
                "log": False,
                "dir_rule": {"rule": "Bd_Aid", "base_dir": self.temp_dir},
                "download": {
                    "cache": True,
                    "image": {"decode": True, "suffix": None},
                    "threading": {"image": 30, "photo": 1},
                },
                "client": {
                    "cache": None,
                    "domain": self._domains if self._domains else [],
                    "postman": {
                        "type": "curl_cffi",
                        "meta_data": {
                            "impersonate": "chrome",
                            "headers": None,
                            "proxies": self._proxy if self._proxy else None,
                        },
                    },
                    "impl": self._client_impl,
                    "retry_times": 3,
                },
            }
            self._option = JmOption.construct(option_dict)
            self._client = self._option.build_jm_client()
            logger.info(f"JMComic 客户端初始化成功 (impl={self._client_impl})")
        except Exception as e:
            logger.error(f"JMComic 客户端初始化失败: {e}")

    # ============ 翻页 ============
    @filter.regex(r"^(\+|下一页|\-|上一页)$")
    async def page_nav(self, event):
        user_id = event.get_sender_id()
        if not self._cache_valid(user_id):
            return
        event.stop_event()

        cache = self._search_cache[user_id]
        raw = event.message_str.strip()
        if raw in ("+", "下一页"):
            if cache["page"] >= cache["total_pages"] - 1:
                yield event.plain_result("已经是最后一页了。")
                return
            cache["page"] += 1
        else:
            if cache["page"] <= 0:
                yield event.plain_result("已经是第一页了。")
                return
            cache["page"] -= 1

        async for result in self._build_search_page(event, cache):
            yield result

    # ============ 工具方法 ============
    def _parse_kw(self, msg: str, prefix: str) -> str:
        clean = msg.strip()
        if clean.startswith("/"):
            clean = clean[1:]
        if clean.startswith(prefix + " "):
            return clean[len(prefix) + 1:].strip()
        if clean == prefix:
            return ""
        if clean.startswith(prefix):
            return clean[len(prefix):].strip()
        return clean

    async def _download_cover(self, album_id: str, save_path: str) -> bool:
        if not self._client:
            return False
        try:
            loop = asyncio.get_event_loop()

            def do_download():
                try:
                    self._client.download_album_cover(album_id, save_path)
                    return True
                except Exception as e:
                    logger.warning(f"封面下载失败: {e}")
                    return False

            return await loop.run_in_executor(None, do_download)
        except Exception as e:
            logger.error(f"封面下载异常: {e}")
            return False

    async def _get_cover_base64(self, album_id: str) -> str | None:
        """获取封面 base64（压缩后，减少体积避免 retcode=1200）"""
        if album_id in self._cover_cache:
            return self._cover_cache[album_id]

        cover_path = os.path.join(self.temp_dir, f"cover_{album_id}.jpg")
        if os.path.exists(cover_path) and os.path.getsize(cover_path) > 0:
            b64 = _file_to_base64_image(cover_path, max_edge=COVER_MAX_EDGE, quality=60)
            self._cover_cache[album_id] = b64
            return b64

        has_cover = await self._download_cover(album_id, cover_path)
        if has_cover and os.path.exists(cover_path) and os.path.getsize(cover_path) > 0:
            b64 = _file_to_base64_image(cover_path, max_edge=COVER_MAX_EDGE, quality=60)
            self._cover_cache[album_id] = b64
            return b64
        return None

    def _search_page_sync(self, page_num: int, keyword: str) -> list:
        try:
            page: JmSearchPage = self._client.search(
                search_query=keyword,
                page=page_num,
                main_tag=0,
                order_by=JmMagicConstants.ORDER_BY_LATEST,
                time=JmMagicConstants.TIME_ALL,
                category=JmMagicConstants.CATEGORY_ALL,
                sub_category=None,
            )
            return [{"album_id": aid, "name": name} for aid, name in page.iter_id_title()]
        except Exception as e:
            logger.error(f"第{page_num}页搜索失败: {e}")
            if page_num == 1:
                raise
            return []

    async def _jm_search(self, keyword: str) -> list:
        if not self._client:
            return []
        try:
            loop = asyncio.get_event_loop()
            tasks = [
                loop.run_in_executor(_thread_pool, self._search_page_sync, p, keyword)
                for p in range(1, MAX_PAGES + 1)
            ]
            pages = await asyncio.gather(*tasks, return_exceptions=True)

            all_results = []
            for p in pages:
                if isinstance(p, Exception):
                    if all_results:
                        break
                    logger.error(f"搜索异常: {p}")
                    return []
                all_results.extend(p)
                if len(all_results) >= MAX_RESULTS:
                    break
            return all_results[:MAX_RESULTS]
        except Exception as e:
            logger.error(f"搜索异常: {e}")
            return []

    async def _jm_get_detail(self, album_id: str) -> JmAlbumDetail | None:
        if not self._client:
            return None
        try:
            loop = asyncio.get_event_loop()

            def do_get():
                try:
                    return self._client.get_album_detail(album_id)
                except Exception as e:
                    logger.error(f"获取详情失败: {e}")
                    return None

            return await loop.run_in_executor(None, do_get)
        except Exception as e:
            logger.error(f"获取详情异常: {e}")
            return None

    def _build_detail_text(self, album: JmAlbumDetail) -> str:
        text = f"{album.name}\n"
        text += f"ID: JM{album.album_id}\n"
        if album.authors:
            text += f"作者: {', '.join(album.authors[:5])}\n"
        if album.tags:
            text += f"标签: {', '.join(album.tags[:8])}\n"

        chapters = []
        for ep in album.episode_list:
            pid, pindex, pname = ep[0], ep[1], ep[2]
            chapters.append(f"第{pindex}话 {pname} (id:{pid})")

        if chapters:
            text += "\n章节列表:\n"
            text += "\n".join(chapters)
            text += "\n\n/jm看图 <章节序号> 或 /jm看图 <全局序号>"

        return text

    async def _build_search_page(self, event, cache: dict):
        try:
            page = cache["page"]
            total_pages = cache["total_pages"]
            start = page * PAGE_SIZE
            end = min(start + PAGE_SIZE, len(cache["results"]))
            page_results = cache["results"][start:end]

            node_list = []
            for i, r in enumerate(page_results):
                album_id = r["album_id"]
                name = r.get("name", "未知")
                cover_b64 = await self._get_cover_base64(album_id)
                content = []
                if cover_b64:
                    content.append(Image(file=cover_b64))
                content.append(Plain(f"{start + i + 1}. {name}"))
                node_list.append(Node(uin=event.get_self_id(), name="JMComic", content=content))

            node_list.append(Node(
                uin=event.get_self_id(),
                name="JMComic",
                content=[Plain(
                    f"「{cache['keyword']}」第{page + 1}/{total_pages}页\n"
                    f"/jm <序号>看详情  +下一页  -上一页  /jm看图 <序号>"
                )],
            ))

            nodes = Nodes(node_list)
            yield event.chain_result([nodes])
        except Exception as e:
            logger.error(f"构建搜索页失败: {e}")
            yield event.plain_result("构建搜索结果失败。")

    # ============ /jm ============
    @filter.command("jm")
    async def jm_search(self, event):
        if not HAS_JMCOMIC:
            yield event.plain_result("JMComic 库未安装，请联系管理员安装 jmcomic。")
            return
        if not self._client:
            yield event.plain_result("JMComic 客户端未初始化，请检查配置。")
            return

        keyword = self._parse_kw(event.message_str, "jm")
        if not keyword:
            yield event.plain_result("请输入搜索关键词，例如：/jm 全彩")
            return
        if keyword.startswith("看图"):
            yield event.plain_result("请使用 /jm看图 <序号> 或 /jm <关键词>。")
            return

        event.stop_event()

        # 纯数字：判断是否有有效搜索缓存
        if keyword.isdigit():
            if self._cache_valid(event.get_sender_id()):
                # 有缓存 → 全局序号查详情
                cache = self._search_cache[event.get_sender_id()]
                n = int(keyword)
                if 1 <= n <= len(cache["results"]):
                    async for result in self._jm_show_detail(event, cache["results"][n - 1]["album_id"]):
                        yield result
                else:
                    yield event.plain_result(f"序号超出范围（共 {len(cache['results'])} 个结果）。")
            else:
                # 无缓存 → 禁漫ID直接查
                async for result in self._jm_show_detail(event, keyword):
                    yield result
            return

        results = await self._jm_search(keyword)
        if not results:
            yield event.plain_result(f"未找到与「{keyword}」相关的本子。")
            return

        user_id = event.get_sender_id()
        total_pages = (len(results) + PAGE_SIZE - 1) // PAGE_SIZE
        self._search_cache[user_id] = {
            "results": results, "page": 0,
            "total_pages": total_pages, "keyword": keyword,
            "expires_at": time.time() + CACHE_TTL,
        }
        self._cover_cache.clear()

        yield event.plain_result(f"搜索「{keyword}」，找到 {len(results)} 个结果：")
        async for result in self._build_search_page(event, self._search_cache[user_id]):
            yield result

    async def _jm_show_detail(self, event, album_id: str):
        """显示本子详情（封面base64+文本，Nodes合并转发）。"""
        album = await self._jm_get_detail(album_id)
        if not album:
            yield event.plain_result(f"未找到本子 JM{album_id}")
            return

        user_id = event.get_sender_id()
        self._photo_cache[user_id] = album
        text = self._build_detail_text(album)

        cover_path = os.path.join(self.temp_dir, f"cover_{album_id}.jpg")
        has_cover = await self._download_cover(album_id, cover_path)

        node_content = []
        if has_cover and os.path.exists(cover_path) and os.path.getsize(cover_path) > 0:
            node_content.append(Image(file=_file_to_base64_image(cover_path, max_edge=COVER_MAX_EDGE, quality=60)))
        node_content.append(Plain(text))

        nodes = Nodes([])
        nodes.nodes.append(Node(uin=event.get_self_id(), name="JMComic", content=node_content))

        try:
            yield event.chain_result([nodes])
        finally:
            if has_cover and os.path.exists(cover_path):
                try:
                    os.remove(cover_path)
                except Exception:
                    pass

    # ============ /jm看图 ============
    @filter.command("jm看图")
    async def jm_view_photo(self, event):
        if not HAS_JMCOMIC:
            yield event.plain_result("JMComic 库未安装，请联系管理员安装 jmcomic。")
            return
        if not self._client:
            yield event.plain_result("JMComic 客户端未初始化，请检查配置。")
            return

        keyword = self._parse_kw(event.message_str, "jm看图")
        if not keyword or not keyword.isdigit():
            yield event.plain_result("请输入序号 或 章节ID，例如：/jm看图 1")
            return

        event.stop_event()

        user_id = event.get_sender_id()
        n = int(keyword)
        photo_id = None
        chapter_name = ""

        # 优先：有缓存 → 全局第N本 → 下载第1话
        if self._cache_valid(user_id):
            cache = self._search_cache[user_id]
            if 1 <= n <= len(cache["results"]):
                album_info = cache["results"][n - 1]
                album_id = album_info["album_id"]
                album_name = album_info.get("name", f"JM{album_id}")
                album = await self._jm_get_detail(album_id)
                if album and album.episode_list:
                    self._photo_cache[user_id] = album
                    ep = album.episode_list[0]
                    photo_id = ep[0]
                    chapter_name = f"{album_name} 第{ep[1]}话 {ep[2]}"
            else:
                yield event.plain_result(f"序号超出范围（共 {len(cache['results'])} 个结果）。")
                return

        # 无缓存时的备用逻辑
        if not photo_id:
            album: JmAlbumDetail | None = self._photo_cache.get(user_id)
            if album and 1 <= n <= len(album.episode_list):
                ep = album.episode_list[n - 1]
                photo_id = ep[0]
                chapter_name = f"第{ep[1]}话 {ep[2]}"
            if not photo_id:
                photo_id = keyword
                chapter_name = f"JM{photo_id}"

        if not photo_id:
            yield event.plain_result("请先使用 /jm 搜索，或 /jm <本子ID> 查看详情。")
            return

        dl_dir = os.path.join(self.temp_dir, f"photo_{photo_id}_{uuid.uuid4().hex[:8]}")
        os.makedirs(dl_dir, exist_ok=True)
        pdf_path = None

        try:
            loop = asyncio.get_event_loop()

            def do_download():
                try:
                    temp_opt = self._option.copy_option()
                    temp_opt.dir_rule.base_dir = dl_dir
                    temp_opt.dir_rule.rule_dsl = "Bd"
                    temp_opt.download.threading.image = 30
                    jmcomic.download_photo(photo_id, temp_opt, check_exception=False)
                    return True
                except Exception as e:
                    logger.error(f"下载章节失败: {e}")
                    return False

            success = await loop.run_in_executor(None, do_download)
            if not success:
                yield event.plain_result(f"章节 {chapter_name} 下载失败。")
                return

            exts = ("*.jpg", "*.jpeg", "*.png", "*.webp", "*.gif", "*.bmp")
            saved_files = []
            for ext in exts:
                saved_files.extend(glob.glob(os.path.join(dl_dir, "**", ext), recursive=True))
            saved_files = sorted(saved_files)

            total = len(saved_files)
            if total == 0:
                yield event.plain_result(f"章节 {chapter_name} 下载完成但未找到图片文件。")
                return

            compressed_dir = os.path.join(dl_dir, "compressed")
            saved_files = self._compress_images(saved_files, compressed_dir, quality=75, max_edge=1600)

            for old_pdf in glob.glob(os.path.join(self.temp_dir, f"{user_id}_*.pdf")):
                try:
                    os.remove(old_pdf)
                except Exception:
                    pass

            safe_name = f"{user_id}_{uuid.uuid4().hex[:8]}.pdf"
            pdf_path = os.path.join(self.temp_dir, safe_name)
            if not self._images_to_pdf(saved_files, pdf_path):
                yield event.plain_result("PDF 生成失败。")
                return

            yield event.plain_result(f"共 {total} 张，{chapter_name}.pdf")
            yield event.chain_result([File(file=pdf_path, name=f"{chapter_name}.pdf")])

        except Exception as e:
            logger.error(f"看图异常: {e}")
            yield event.plain_result(f"获取图片失败: {e}")
        finally:
            try:
                shutil.rmtree(dl_dir, ignore_errors=True)
            except Exception:
                pass
            if pdf_path and os.path.exists(pdf_path):
                asyncio.create_task(self._delayed_cleanup(pdf_path))

    # ============ Agent 工具（LLM 可调用）============

    @filter.llm_tool(name="search_JMComic")
    async def agent_search(self, event, keyword: str):
        """搜索禁漫本子，返回按最新更新排序的前10个结果。

        Args:
            keyword(string): 搜索关键词，如 少女、全彩 等
        """
        if not HAS_JMCOMIC or not self._client:
            yield event.plain_result("JMComic 服务不可用。")
            return
        results = await self._jm_search(keyword)
        if not results:
            yield event.plain_result(f"未找到与「{keyword}」相关的本子。")
            return
        lines = [f"搜索「{keyword}」，找到 {len(results)} 个结果："]
        for i, r in enumerate(results[:10]):
            lines.append(f"{i + 1}. [{r['album_id']}] {r['name']}")
        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="get_JMComic_detail")
    async def agent_detail(self, event, album_id: str):
        """获取禁漫本子详情，以合并转发形式返回封面+本子名+作者+标签+章节列表。

        Args:
            album_id(string): 禁漫本子ID，如 1446388
        """
        if not HAS_JMCOMIC or not self._client:
            yield event.plain_result("JMComic 服务不可用。")
            return
        album = await self._jm_get_detail(album_id)
        if not album:
            yield event.plain_result(f"未找到本子 JM{album_id}")
            return
        text = self._build_detail_text(album)

        cover_path = os.path.join(self.temp_dir, f"cover_{album_id}.jpg")
        has_cover = await self._download_cover(album_id, cover_path)

        node_content = []
        if has_cover and os.path.exists(cover_path) and os.path.getsize(cover_path) > 0:
            node_content.append(Image(file=_file_to_base64_image(cover_path, max_edge=COVER_MAX_EDGE, quality=60)))
        node_content.append(Plain(text))

        nodes = Nodes([])
        nodes.nodes.append(Node(uin=event.get_self_id(), name="JMComic", content=node_content))

        try:
            yield event.chain_result([nodes])
        finally:
            if has_cover and os.path.exists(cover_path):
                try:
                    os.remove(cover_path)
                except Exception:
                    pass

    @filter.llm_tool(name="download_JMComic_chapter")
    async def agent_download(self, event, photo_id: str):
        """下载禁漫指定章节的所有图片并拼接为 PDF 文件发送。

        Args:
            photo_id(string): 章节ID，从详情中的章节列表获取，如 1446388
        """
        if not HAS_JMCOMIC or not self._client:
            yield event.plain_result("JMComic 服务不可用。")
            return

        dl_dir = os.path.join(self.temp_dir, f"photo_{photo_id}_{uuid.uuid4().hex[:8]}")
        os.makedirs(dl_dir, exist_ok=True)
        pdf_path = None

        try:
            loop = asyncio.get_event_loop()

            def do_download():
                try:
                    temp_opt = self._option.copy_option()
                    temp_opt.dir_rule.base_dir = dl_dir
                    temp_opt.dir_rule.rule_dsl = "Bd"
                    temp_opt.download.threading.image = 30
                    jmcomic.download_photo(photo_id, temp_opt, check_exception=False)
                    return True
                except Exception as e:
                    logger.error(f"下载章节失败: {e}")
                    return False

            success = await loop.run_in_executor(None, do_download)
            if not success:
                yield event.plain_result(f"章节 JM{photo_id} 下载失败。")
                return

            exts = ("*.jpg", "*.jpeg", "*.png", "*.webp", "*.gif", "*.bmp")
            saved_files = []
            for ext in exts:
                saved_files.extend(glob.glob(os.path.join(dl_dir, "**", ext), recursive=True))
            saved_files = sorted(saved_files)

            total = len(saved_files)
            if total == 0:
                yield event.plain_result(f"章节 JM{photo_id} 下载完成但未找到图片文件。")
                return

            compressed_dir = os.path.join(dl_dir, "compressed")
            saved_files = self._compress_images(saved_files, compressed_dir)

            safe_name = f"agent_{uuid.uuid4().hex[:8]}.pdf"
            pdf_path = os.path.join(self.temp_dir, safe_name)
            if not self._images_to_pdf(saved_files, pdf_path):
                yield event.plain_result("PDF 生成失败。")
                return

            yield event.plain_result(f"共 {total} 张，JM{photo_id}.pdf")
            yield event.chain_result([File(file=pdf_path, name=f"JM{photo_id}.pdf")])

        except Exception as e:
            logger.error(f"Agent 下载异常: {e}")
            yield event.plain_result(f"下载失败: {e}")
        finally:
            try:
                shutil.rmtree(dl_dir, ignore_errors=True)
            except Exception:
                pass
            if pdf_path and os.path.exists(pdf_path):
                asyncio.create_task(self._delayed_cleanup(pdf_path))

    @staticmethod
    async def _delayed_cleanup(filepath: str, delay: float = 3.0):
        await asyncio.sleep(delay)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass

    @staticmethod
    def _compress_images(image_paths: list, output_dir: str, quality: int = 75, max_edge: int = 1600) -> list:
        try:
            from PIL import Image as PILImage
        except ImportError:
            return image_paths

        os.makedirs(output_dir, exist_ok=True)
        compressed = []
        for path in image_paths:
            try:
                img = PILImage.open(path)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                w, h = img.size
                if max(w, h) > max_edge:
                    ratio = max_edge / max(w, h)
                    img = img.resize((int(w * ratio), int(h * ratio)), PILImage.LANCZOS)
                basename = os.path.splitext(os.path.basename(path))[0] + ".jpg"
                out_path = os.path.join(output_dir, basename)
                img = img.convert("RGB")
                img.save(out_path, "JPEG", quality=quality, optimize=True)
                img.close()
                compressed.append(out_path)
            except Exception as e:
                logger.warning(f"压缩图片失败 {path}: {e}，使用原图")
                compressed.append(path)
        return compressed

    @staticmethod
    def _images_to_pdf(image_paths: list, output_path: str) -> bool:
        if not image_paths:
            return False
        try:
            import img2pdf
            with open(output_path, "wb") as f:
                f.write(img2pdf.convert(image_paths))
            return True
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"img2pdf 转换失败，回退到 Pillow: {e}")

        try:
            from PIL import Image as PILImage
            chunk_size = 50
            chunks = [image_paths[i:i + chunk_size] for i in range(0, len(image_paths), chunk_size)]
            temp_pdfs = []
            for ci, chunk in enumerate(chunks):
                images = []
                for path in chunk:
                    img = PILImage.open(path)
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    images.append(img)
                chunk_pdf = output_path + f".chunk{ci}.pdf"
                if len(images) == 1:
                    images[0].save(chunk_pdf, "PDF")
                else:
                    images[0].save(chunk_pdf, "PDF", save_all=True, append_images=images[1:])
                for img in images:
                    img.close()
                temp_pdfs.append(chunk_pdf)
            if len(temp_pdfs) == 1:
                shutil.move(temp_pdfs[0], output_path)
            else:
                JMComicPlugin._merge_pdfs(temp_pdfs, output_path)
                for tp in temp_pdfs:
                    try:
                        os.remove(tp)
                    except Exception:
                        pass
            return True
        except ImportError:
            logger.error("Pillow 库未安装，无法生成 PDF。")
            return False
        except Exception as e:
            logger.error(f"PDF 生成失败: {e}")
            return False

    @staticmethod
    def _merge_pdfs(input_paths: list, output_path: str) -> bool:
        try:
            from PyPDF2 import PdfMerger
            merger = PdfMerger()
            for path in input_paths:
                merger.append(path)
            merger.write(output_path)
            merger.close()
            return True
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"PyPDF2 合并失败: {e}")
        logger.error("无法合并 PDF 分块，请安装 PyPDF2。")
        return False