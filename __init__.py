import os
import re
import time
import random
import traceback
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional

import requests
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from bs4 import BeautifulSoup
from xml.etree import ElementTree as ET

from app.core.config import settings
from app.plugins import _PluginBase
from app.log import logger
from app.schemas import NotificationType


class st98sign(_PluginBase):
    # 插件名称
    plugin_name = "98签到"
    # 插件描述
    plugin_desc = "自动完成98堂每日签到和帖子回复，支持自定义Host和代理。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/Ferdi_B.png"
    # 插件版本
    plugin_version = "1.1.0"
    # 插件作者
    plugin_author = "Desire"
    # 作者主页
    author_url = "https://github.com"
    # 插件配置项ID前缀
    plugin_config_prefix = "st98sign_"
    # 加载顺序
    plugin_order = 11
    # 可使用的用户级别
    auth_level = 2
    # 插件类型和根目录
    plugin_type = "web"
    plugin_dir = "st98sign"

    # --- 私有属性 ---
    _enabled = False
    _cookie = None
    _host = None
    _proxy = None
    _notify = False
    _sign_cron = None
    _reply_cron = None
    _sign_onlyonce = False
    _reply_onlyonce = False
    _reply_fid = 103  # 默认高清中文字幕区
    _reply_times = 1 # 添加私有属性
    _auto_replies_str = "" # 存储原始的回复文本
    _auto_replies = []     # 解析后的回复列表
    _history_days = 30

    _scheduler_sign: Optional[BackgroundScheduler] = None
    _scheduler_reply: Optional[BackgroundScheduler] = None
    _manual_trigger_sign = False
    _manual_trigger_reply = False

    # --- 默认值 ---
    DEFAULT_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    DEFAULT_REPLIES = (
        '感谢分享。',
        '谢谢楼主。',
        '内容不错，收藏备用。',
        '感谢提供这么好的资源。',
        '好看好看',
        '非常感谢。',
        '什么都不说了，保存!',
        '进来看看，顺便支持一下。',
        '挺不错的，已保存，谢谢。',
        '就喜欢这种，真有味道',
        '支持好内容！',
        '回复支持一下楼主。',
        '找了这个好久，赶紧收藏住!',
        '这个必须顶！',
        '谢谢分享！',
    )

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        logger.info("========== st98sign 初始化 ==========")
        try:
            # 如果config为None，尝试主动获取配置
            if config is None:
                logger.warning("init_plugin 未接收到 config 参数，尝试主动获取配置...")
                try:
                    config = self.get_config()
                    if config:
                        logger.info("成功主动获取到配置信息")
                    else:
                        logger.warning("主动获取配置失败或配置为空")
                except Exception as get_config_e:
                    logger.error(f"主动获取配置时出错: {get_config_e}")
                    config = {}

            if config:
                self._enabled = config.get("enabled", False)
                self._cookie = config.get("cookie")
                self._host = config.get("host")
                self._proxy = config.get("proxy")
                self._notify = config.get("notify", False)
                self._sign_cron = config.get("sign_cron")
                self._reply_cron = config.get("reply_cron")
                self._sign_onlyonce = config.get("sign_onlyonce", False)
                self._reply_onlyonce = config.get("reply_onlyonce", False)
                # 添加对 int 转换的保护
                try:
                    self._reply_fid = int(config.get("reply_fid", 103))
                except (ValueError, TypeError):
                    logger.warning(f"配置中的 reply_fid 无效，将使用默认值 103。原始值: {config.get('reply_fid')}")
                    self._reply_fid = 103
                # 添加对 reply_times 的读取和保护
                try:
                    self._reply_times = int(config.get("reply_times", 1))
                    if self._reply_times < 1:
                         logger.warning(f"配置中的 reply_times ({self._reply_times}) 无效，将使用默认值 1。")
                         self._reply_times = 1
                except (ValueError, TypeError):
                    logger.warning(f"配置中的 reply_times 无效，将使用默认值 1。原始值: {config.get('reply_times')}")
                    self._reply_times = 1
                self._auto_replies_str = config.get("auto_replies", "")
                # 添加对 int 转换的保护
                try:
                    self._history_days = int(config.get("history_days", 30))
                except (ValueError, TypeError):
                     logger.warning(f"配置中的 history_days 无效，将使用默认值 30。原始值: {config.get('history_days')}")
                     self._history_days = 30

                # 解析自定义回复内容
                if self._auto_replies_str:
                    # 使用普通换行符分割，而不是 \n
                    self._auto_replies = [line.strip() for line in self._auto_replies_str.split('\n') if line.strip()]
                else:
                    self._auto_replies = list(self.DEFAULT_REPLIES)

                logger.info(f"配置加载完成: enabled={self._enabled}, notify={self._notify}, host={self._host}, proxy={self._proxy}, "
                            f"sign_cron={self._sign_cron}, reply_cron={self._reply_cron}, "
                            f"reply_fid={self._reply_fid}, history_days={self._history_days}, "
                            f"auto_replies_count={len(self._auto_replies)}, reply_times={self._reply_times}")
            else:
                 logger.warning("未接收到配置信息 (config is None)")

            # 处理立即执行 - 签到
            if self._sign_onlyonce:
                logger.info("执行一次性签到")
                self._manual_trigger_sign = True
                self._scheduler_sign = BackgroundScheduler(timezone=settings.TZ)
                # 稍微延迟几秒，确保在回复任务之后执行
                self._scheduler_sign.add_job(func=self.sign, trigger='date',
                                         run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=10),
                                         id="st98sign_sign_once",
                                         name="98堂立即签到")
                if self._scheduler_sign.get_jobs():
                    self._scheduler_sign.start()
                # 重置标志并更新配置
                self._sign_onlyonce = False
                self.update_config(self._get_current_config(sign_onlyonce=False))

            # 处理立即执行 - 回复
            if self._reply_onlyonce:
                logger.info("执行一次性回复")
                self._manual_trigger_reply = True
                self._scheduler_reply = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler_reply.add_job(func=self.reply, trigger='date',
                                          run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3), # 先执行回复
                                          id="st98sign_reply_once",
                                          name="98堂立即回复")
                if self._scheduler_reply.get_jobs():
                    self._scheduler_reply.start()
                # 重置标志并更新配置
                self._reply_onlyonce = False
                self.update_config(self._get_current_config(reply_onlyonce=False))

        except Exception as e:
            logger.error(f"st98sign 初始化错误: {str(e)}", exc_info=True)

    def _get_current_config(self, **kwargs):
        """ 获取当前配置并允许覆盖特定值 """
        config = {
            "enabled": self._enabled,
            "cookie": self._cookie,
            "host": self._host,
            "proxy": self._proxy,
            "notify": self._notify,
            "sign_cron": self._sign_cron,
            "reply_cron": self._reply_cron,
            "sign_onlyonce": self._sign_onlyonce,
            "reply_onlyonce": self._reply_onlyonce,
            "reply_fid": self._reply_fid,
            "reply_times": self._reply_times,
            "auto_replies": self._auto_replies_str,
            "history_days": self._history_days
        }
        config.update(kwargs)
        return config

    def _parse_cookies(self) -> dict:
        """ 解析 Cookie 字符串为字典 """
        cookies = {}
        if not self._cookie:
            return cookies
        try:
            for item in self._cookie.split(';'):
                if '=' in item:
                    name, value = item.strip().split('=', 1)
                    cookies[name] = value
        except Exception as e:
            logger.error(f"解析Cookie时出错: {str(e)}")
        return cookies

    def _get_proxies(self) -> Optional[dict]:
        """ 获取代理配置 """
        if not self._proxy:
            return None
        # 支持 http 和 https 代理
        if self._proxy.startswith("http://") or self._proxy.startswith("https://"):
            return {"http": self._proxy, "https": self._proxy}
        # 支持 socks5 代理 (需要安装 requests[socks])
        elif self._proxy.startswith("socks5://"):
             try:
                 import socks # noqa F401
                 return {"http": self._proxy, "https": self._proxy}
             except ImportError:
                 logger.error("检测到 SOCKS5 代理，但缺少必要的依赖。请运行 pip install requests[socks]")
                 return None
        else:
            logger.warning(f"不支持的代理格式: {self._proxy}，请使用 http://, https:// 或 socks5:// 前缀")
            return None

    def _request_session(self, session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
        """ 使用传入的 requests.Session 发起请求 """
        full_url = url # 假设传入的已经是完整URL或者相对路径处理在外部完成
        logger.debug(f"_request_session: 请求 {method.upper()} {full_url}")

        # 移除 requests 不直接支持的 httpx 参数 (如果从旧代码迁移过来)
        kwargs.pop('http2', None)

        # 确保 User-Agent 存在
        headers = kwargs.get('headers', {})
        headers.setdefault('User-Agent', self.DEFAULT_USER_AGENT)
        kwargs['headers'] = headers

        # 设置超时
        kwargs.setdefault('timeout', 30.0)
        # 允许重定向是requests的默认行为，无需显式设置 follow_redirects

        try:
            response = session.request(method, full_url, **kwargs)
            logger.debug(f"_request_session: 响应状态码 {response.status_code} for {full_url}")
            response.raise_for_status() # 对 >= 400 的状态码抛出异常
            return response
        except requests.exceptions.Timeout as timeout_err:
            logger.error(f"_request_session: 请求超时: {method.upper()} {full_url} - {timeout_err}", exc_info=True)
            raise
        except requests.exceptions.HTTPError as http_err:
            # 日志记录已包含状态码，这里记录更详细的错误
            logger.error(f"_request_session: HTTP 错误: {method.upper()} {full_url} - {http_err}", exc_info=True)
            # 可以选择记录响应体的前一部分用于调试
            if http_err.response is not None:
                 logger.error(f"_request_session: HTTP 错误响应内容 (前100字符): {http_err.response.text[:100]}")
            raise
        except requests.exceptions.RequestException as req_exc:
            logger.error(f"_request_session: 请求失败 ({type(req_exc).__name__}): {method.upper()} {full_url} - {req_exc}", exc_info=True)
            raise
        except Exception as e:
            logger.error(f"_request_session: 处理请求时发生意外错误: {method.upper()} {full_url} - {e}", exc_info=True)
            raise

    def _perform_operation(self, operation_func: callable, **kwargs):
        """ 创建会话、处理通用逻辑（代理、Cookie、年龄确认）并执行具体操作 """
        if not self._host:
            raise ValueError("站点Host未配置")

        base_url = f"https://{self._host}"
        session = requests.Session()

        # 1. 设置 Cookie
        cookies = self._parse_cookies()
        if cookies:
            session.cookies.update(cookies)
            logger.debug(f"_perform_operation: Session Cookie 已设置: {session.cookies.get_dict()}")
        else:
             logger.warning("_perform_operation: 未能解析或未配置Cookie")

        # 2. 设置代理
        proxies = self._get_proxies()
        if proxies:
            session.proxies.update(proxies)
            logger.debug(f"_perform_operation: Session 代理已设置: {session.proxies}")

        # 3. 设置通用请求头
        session.headers.update({'User-Agent': self.DEFAULT_USER_AGENT})

        # 4. 年龄确认处理 (如果需要)
        try:
            logger.debug("_perform_operation: 开始年龄确认检查")
            if '_safe' not in session.cookies.get_dict():
                logger.info("_perform_operation: Cookie 中缺少 _safe，尝试进行年龄确认...")
                age_confirmed = False
                age_retry_cnt = 3
                while not age_confirmed and age_retry_cnt > 0:
                    home_url = f"{base_url}/"
                    headers_age = {'Referer': base_url + '/'}
                    try:
                        r_age = self._request_session(session, 'get', home_url, headers=headers_age)
                        if (v := re.findall(r"safeid='(\w+)'", r_age.text, re.MULTILINE | re.IGNORECASE)):
                            safeid = v[0]
                            logger.info(f'_perform_operation: 自动设置年龄确认Cookie: _safe={safeid}')
                            # requests 需要手动设置 cookie 及其 domain
                            session.cookies.set(name='_safe', value=safeid, domain=self._host)
                            age_confirmed = True
                            break # 成功后退出循环
                        elif "forum.php" in r_age.text: # 假设看到论坛链接说明已通过
                            logger.debug("_perform_operation: 页面包含论坛链接，认为已通过年龄确认")
                            age_confirmed = True
                            break # 成功后退出循环
                        else:
                            logger.warning("_perform_operation: 年龄确认尝试失败，未找到 safeid 或论坛链接")

                    except requests.exceptions.RequestException as age_req_e:
                        logger.error(f"_perform_operation: 年龄确认请求失败: {age_req_e}", exc_info=True)
                        # 不立即退出，允许重试
                    except Exception as age_inner_e:
                        logger.error(f"_perform_operation: 年龄确认内部处理失败: {age_inner_e}", exc_info=True)
                        # 不立即退出，允许重试

                    age_retry_cnt -= 1
                    if age_retry_cnt > 0:
                         logger.debug(f"_perform_operation: 年龄确认重试，剩余 {age_retry_cnt} 次")
                         time.sleep(1)

                if not age_confirmed:
                    logger.error('_perform_operation: 多次尝试后未能通过年龄确认')
                    # 根据策略，可以选择在这里抛出异常或继续执行
                    # raise Exception("未能通过年龄确认")
                else:
                     logger.info("_perform_operation: 年龄确认成功或已确认")
            else:
                logger.debug("_perform_operation: Cookie 中已存在 _safe，跳过年龄确认")

        except Exception as age_e:
            logger.error(f"_perform_operation: 年龄确认过程中发生意外错误: {age_e}", exc_info=True)
            # 记录错误，但通常不应阻止后续操作

        # 5. 执行具体操作
        try:
            logger.debug(f"_perform_operation: 准备调用操作函数 {operation_func.__name__}")
            result = operation_func(session, base_url=base_url, **kwargs)
            logger.debug(f"_perform_operation: 操作函数 {operation_func.__name__} 执行完毕")
            return result
        except Exception as op_e:
             logger.error(f"_perform_operation: 执行操作 {operation_func.__name__} 时出错: {op_e}", exc_info=True)
             raise # 将操作中的异常向上抛出

    def _preprocess_xml_text(self, text) -> str:
        """ 处理 Discuz 返回的 XML CDATA 中的 HTML """
        if not text or not isinstance(text, str):
            return str(text) if text else ""
        # 清理 XML 和脚本文本，使其更易读
        # 针对特定的成功消息模式进行处理
        if '签到成功' in text:
            success_match = re.search(r'签到成功[^\'\"]*', text)
            if success_match:
                return success_match.group(0)

        if '回复发布成功' in text:
            return '回复发布成功'
            
        # 如果没有特定匹配，进行一般处理
        if '<![CDATA[' not in text:
            # 清理可能的 XML 标签
            cleaned = re.sub(r'<[^>]+>', ' ', text)
            # 删除多余空格
            cleaned = re.sub(r'\s+', ' ', cleaned).strip()
            return cleaned

        try:
            root = ET.fromstring(text)
            cdata = root.text
            if not cdata: return text
            soup = BeautifulSoup(cdata, 'lxml') # 使用 lxml 解析器
            # 移除脚本和样式
            for script in soup.find_all(['script', 'style']):
                script.decompose()
            # 获取纯文本，并进一步清理
            text_content = soup.get_text(separator=' ', strip=True)
            
            # 针对常见的成功消息模式进行特殊处理
            if '签到成功' in text_content:
                success_match = re.search(r'签到成功[^\'\"]*', text_content)
                if success_match:
                    return success_match.group(0)
                
            if '回复发布成功' in text_content:
                return '回复发布成功'
            
            # 删除多余空格
            cleaned = re.sub(r'\s+', ' ', text_content).strip()
            return cleaned or "操作已完成" # 返回处理后的文本或简单确认
        except Exception as e:
            logger.warning(f"处理XML响应时出错: {e} - 原始文本: {text[:100]}...")
            # 尝试提取 CDATA 内容
            match = re.search(r'CDATA\[(.*?)\]\]>', text, re.DOTALL)
            if match:
                content = match.group(1).strip()
                # 清理脚本和HTML标签
                content = re.sub(r'<script.*?</script>', '', content, flags=re.DOTALL)
                content = re.sub(r'<[^>]+>', ' ', content)
                # 针对成功消息进行特殊处理
                if '签到成功' in content:
                    success_match = re.search(r'签到成功[^\'\"]*', content)
                    if success_match:
                        return success_match.group(0)
                
                if '回复发布成功' in content:
                    return '回复发布成功'
                
                # 删除多余空格并返回
                return re.sub(r'\s+', ' ', content).strip() or "操作已完成"
            
            # 清理原始文本并返回
            cleaned = re.sub(r'<[^>]+>', ' ', text)
            cleaned = re.sub(r'\s+', ' ', cleaned).strip()
            return cleaned or "操作已完成"

    # --- 签到逻辑 ---
    def sign(self):
        """ 执行签到 """
        logger.info("============= 开始 ST98 签到 =============")
        task_type = "签到"
        history_key = f"{self.plugin_config_prefix}{task_type}_history"

        # 检查是否启用
        if not self._enabled:
            logger.info("插件未启用，跳过签到任务。")
            return

        # --- 添加随机延迟 (仅针对定时任务) ---
        is_manual = self._manual_trigger_sign
        trigger_type = "手动触发" if is_manual else "定时触发"
        if not is_manual:
            delay = random.uniform(5, 300)
            logger.info(f"定时任务触发 (签到)，随机延迟 {delay:.2f} 秒后执行...")
            time.sleep(delay)

        # 检查今天是否已执行（仅针对非手动触发）
        if not is_manual and self._is_already_done_today(history_key):
            logger.info("根据历史记录，今日已签到，跳过本次执行")
            return

        # 重置手动触发标志
        self._manual_trigger_sign = False

        # --- 执行核心操作 ---
        sign_dict = { # 初始化历史记录字典
            "date": datetime.now(tz=pytz.timezone(settings.TZ)).strftime('%Y-%m-%d %H:%M:%S'),
            "status": "未知",
            "message": "",
            "trigger": trigger_type,
            "reward_amount": None,
            "username": None,      # 添加 username
            "points_before": None, # 添加签到前积分
            "money_before": None   # 添加签到前金钱
        }

        try:
            # 检查Cookie和Host (基础检查)
            if not self._cookie or not self._host:
                raise ValueError("Cookie 或 站点Host 未配置")

            # --- 步骤1: 获取签到前用户信息 ---
            logger.info("开始获取签到前用户信息...")
            user_info_before = self._perform_operation(self._get_user_info)
            if user_info_before:
                sign_dict["username"] = user_info_before.get("username")
                sign_dict["points_before"] = user_info_before.get("points")
                sign_dict["money_before"] = user_info_before.get("money")
                logger.info(f"获取到用户信息: 用户名={sign_dict['username']}, 积分={sign_dict['points_before']}, 金钱={sign_dict['money_before']}")
            else:
                logger.warning("未能获取到签到前用户信息")

            # --- 步骤2: 执行签到操作 ---
            logger.info("开始执行签到操作...")
            sign_result = self._perform_operation(self._sign_internal)
            sign_dict.update(sign_result) # 将签到结果合并到 sign_dict

        except ValueError as ve: # 配置错误
             logger.error(f"签到配置错误: {ve}")
             sign_dict["status"] = "配置错误"
             sign_dict["message"] = str(ve)
        except requests.exceptions.RequestException as req_err: # 网络错误
            logger.error(f"签到请求失败: {req_err}", exc_info=True)
            sign_dict["status"] = "请求失败"
            sign_dict["message"] = f"网络错误: {req_err}"
            if isinstance(req_err, requests.exceptions.HTTPError) and req_err.response is not None:
                sign_dict["message"] = f"HTTP {req_err.response.status_code}: {self._preprocess_xml_text(req_err.response.text)}"
        except Exception as e:
            logger.error(f"签到过程中发生错误: {e}", exc_info=True)
            sign_dict["status"] = "执行出错"
            sign_dict["message"] = str(e)
        finally:
            # 保存包含用户信息和签到结果的完整历史
            sign_dict['trigger'] = trigger_type # 确保 trigger 总是设置
            sign_dict['date'] = datetime.now(tz=pytz.timezone(settings.TZ)).strftime('%Y-%m-%d %H:%M:%S') # 确保时间是最终时间
            self._save_history(history_key, sign_dict)
            if self._notify and sign_dict.get("status") != "跳过":
                self._send_notification(sign_dict, task_type="签到")

            logger.info(f"签到完成 (ST98Sign): 状态={sign_dict['status']}, 消息={sign_dict['message']}")
            if sign_dict["status"] in ["签到成功", "已签到"] and not is_manual:
                 self._save_last_done_date(history_key)

    def _sign_internal(self, session: requests.Session, base_url: str, **kwargs) -> dict:
        """ 实际执行签到的内部逻辑，使用传入的 session """
        result = {"status": "未知", "message": "", "reward_amount": None} # 添加 reward_amount
        formhash = None
        signtoken = None
        action_url = None
        id_hash = None
        ans = None

        sign_plugin_url = f'{base_url}/plugin.php?id=dd_sign&mod=sign'

        # 1. 访问签到页面获取 Cookie 和基础信息 (可选，但模拟流程)
        try:
            self._request_session(session, 'get', sign_plugin_url, headers={'Referer': f'{base_url}/'})
            logger.debug("_sign_internal: 访问初始签到页面成功")
        except requests.exceptions.RequestException as e:
            logger.warning(f"_sign_internal: 访问初始签到页面失败: {e}，继续尝试后续步骤...")

        # 2. 获取签到表单参数 (通过Ajax接口)，包含重试逻辑
        ajax_url = f'{base_url}/plugin.php?id=dd_sign&ac=sign&infloat=yes&handlekey=pc_click_ddsign&inajax=1&ajaxtarget=fwin_content_pc_click_ddsign'
        max_retries = 3
        retry_delay_seconds = 15 # 系统繁忙后等待时间
        form_params_obtained = False
        last_exception = None

        for attempt in range(max_retries):
            try:
                logger.debug(f"尝试获取签到表单参数 (第 {attempt + 1}/{max_retries} 次)")
                r = self._request_session(session, 'get', ajax_url, headers={'Referer': sign_plugin_url})
                raw_xml = r.text
                logger.debug(f"_sign_internal: 获取签到表单Ajax响应: {raw_xml[:200]}...")

                # 检查是否系统繁忙
                if "系统繁忙" in raw_xml:
                    logger.warning(f"获取签到表单参数失败 (第 {attempt + 1} 次): 系统繁忙。将在 {retry_delay_seconds} 秒后重试...")
                    last_exception = Exception("系统繁忙") # 记录最后错误类型
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay_seconds)
                    continue # 继续下一次重试

                # 解析响应
                soup = BeautifulSoup(raw_xml, 'xml')
                html_content = soup.find('root').string if soup.find('root') else None
                if not html_content:
                    processed_text = self._preprocess_xml_text(raw_xml)
                    # 检查是否已签到（可能是无HTML内容的原因）
                    if '已经签到' in processed_text:
                         logger.info(f"_sign_internal: 从Ajax响应检测到已签到: {processed_text}")
                         return {"status": "已签到", "message": processed_text} # 直接返回已签到状态
                    # 如果不是已签到，则抛出异常
                    raise Exception(f"无法从Ajax响应中提取HTML内容。响应内容: {processed_text}")

                root = BeautifulSoup(html_content, 'lxml')
                formhash_input = root.find('input', {'name': 'formhash'})
                signtoken_input = root.find('input', {'name': 'signtoken'})
                action_form = root.find('form', {'name': 'login'})
                secqaa_span = root.find('span', id=re.compile(r'^secqaa_'))

                if not all([formhash_input, signtoken_input, action_form, secqaa_span]):
                    processed_text = self._preprocess_xml_text(raw_xml)
                    missing = []
                    if not formhash_input: missing.append("formhash")
                    if not signtoken_input: missing.append("signtoken")
                    if not action_form: missing.append("form action")
                    if not secqaa_span: missing.append("验证码ID (secqaa)")
                    # 如果缺少参数且不是已签到，则抛出异常
                    if '已经签到' not in processed_text:
                         raise Exception(f"未能从签到表单中提取必要参数: {', '.join(missing)}。响应内容: {processed_text}")
                    else: # 如果缺少参数但提示已签到
                         logger.info(f"_sign_internal: 提取参数时发现已签到提示: {processed_text}")
                         return {"status": "已签到", "message": processed_text}

                formhash = formhash_input['value']
                signtoken = signtoken_input['value']
                # action_url 可能是相对路径，需要拼接 base_url
                action_path = action_form['action'].lstrip('/')
                action_url = f"{base_url}/{action_path}"
                id_hash = secqaa_span['id'].removeprefix('secqaa_')
                logger.info(f"_sign_internal: 获取签到表单参数成功: formhash={formhash}, signtoken={signtoken}, id_hash={id_hash}, action={action_url}")
                form_params_obtained = True
                break # 成功获取，跳出重试循环

            except requests.exceptions.RequestException as e:
                logger.warning(f"获取签到表单参数请求失败 (第 {attempt + 1} 次): {e}")
                last_exception = e
                if attempt < max_retries - 1:
                    time.sleep(5) # 网络请求失败，短暂等待后重试
            except Exception as e:
                logger.error(f"处理签到表单响应时出错 (第 {attempt + 1} 次): {e}", exc_info=True)
                last_exception = e
                # 解析错误通常不建议重试，直接跳出
                break # 退出循环

        # 如果重试完成后仍未成功获取参数
        if not form_params_obtained:
            error_msg = f"多次尝试后未能获取签到表单参数。"
            if last_exception:
                 error_msg += f" 最后错误: {last_exception}"
            raise Exception(error_msg) # 抛出异常，由外层处理

        # 3. 获取验证问题
        qaa_url = f'{base_url}/misc.php?mod=secqaa&action=update&idhash={id_hash}&{round(random.random(), 16)}'
        try:
            r = self._request_session(session, 'get', qaa_url, headers={'Referer': sign_plugin_url})
            qes_rsl = re.findall(r"'(.*?) = \?'", r.text, re.MULTILINE | re.IGNORECASE)
            if not qes_rsl or not qes_rsl[0]:
                raise Exception(f'未能获取到有效的验证问题。响应: {r.text}')
            qes = qes_rsl[0]
            try:
                ans = eval(qes, {"__builtins__": {}}, {})
                if not isinstance(ans, int):
                     raise ValueError("计算结果不是整数")
            except Exception as eval_e:
                 raise Exception(f'计算验证问题 "{qes}" 时出错: {eval_e}')
            logger.info(f'_sign_internal: 获取并计算验证问题成功: {qes} = {ans}')
        except requests.exceptions.RequestException as e:
            raise Exception(f"_sign_internal: 获取验证问题失败: {e}")
        except Exception as e:
            raise Exception(f"_sign_internal: 处理验证问题失败: {e}")

        # 4. 提交签到
        submit_url = f'{action_url}&inajax=1' # action_url 已经是完整的了
        submit_data = {
            'formhash': formhash,
            'signtoken': signtoken,
            'secqaahash': id_hash,
            'secanswer': ans
        }
        try:
            r = self._request_session(session, 'post', submit_url, data=submit_data, headers={'Referer': sign_plugin_url})
            response_text = r.text
            logger.debug(f"_sign_internal: 签到提交响应: {response_text[:200]}...")
            processed_text = self._preprocess_xml_text(response_text)
            logger.info(f"_sign_internal: 签到结果: {processed_text}")

            if '签到成功' in processed_text:
                result["status"] = "签到成功"
                # 尝试提取完整的成功消息
                success_match = re.search(r"(签到成功.+)", processed_text)
                result["message"] = success_match.group(1).strip() if success_match else processed_text
                # 尝试提取奖励数量 (假设格式为 "获得 xx 金钱" 或 "奖励 xx 金钱")
                reward_match = re.search(r'(?:获得|奖励)\s*(\d+)\s*金钱', processed_text)
                if reward_match:
                    try:
                        result["reward_amount"] = int(reward_match.group(1))
                        logger.info(f"提取到签到奖励: {result['reward_amount']} 金钱")
                    except ValueError:
                        logger.warning("无法将提取到的奖励转换为数字")

            elif '已签到' in processed_text:
                result["status"] = "已签到"
                result["message"] = re.search(r"(已经签到.+)", processed_text).group(1).strip() if re.search(r"(已经签到.+)", processed_text) else processed_text
            elif '需要先登录' in processed_text:
                result["status"] = "失败"
                result["message"] = "Cookie无效或已过期"
            else:
                result["status"] = "失败"
                result["message"] = processed_text
        except requests.exceptions.RequestException as e:
            if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
                response_text_processed = self._preprocess_xml_text(e.response.text)
                error_msg = "_sign_internal: 提交签到失败: HTTP {} - {}".format(e.response.status_code, response_text_processed)
                raise Exception(error_msg)
            else:
                raise Exception("提交签到网络请求失败: {}".format(e))
        except Exception as e:
            # 为了确保 try 块总是有 except 子句，保留这个通用的 except
            raise Exception("处理签到响应时失败: {}".format(e))

        return result

    # --- 回复逻辑 ---
    def reply(self):
        """ 执行回复，支持多次 """
        logger.info("============= 开始 ST98 回复 =============")
        task_type = "回复"
        history_key = f"{self.plugin_config_prefix}{task_type}_history"

        # 检查是否启用
        if not self._enabled:
            logger.info("插件未启用，跳过回复任务。")
            return

        # 检查基础配置 (Cookie, Host, 回复内容)
        if not self._cookie or not self._host:
             logger.error("回复失败：Cookie 或 站点Host 未配置。")
             # 可以在这里记录错误历史并返回，或者让 perform_operation 处理
             # 为简单起见，让 perform_operation 处理
             pass # 继续执行，让 _perform_operation 抛出或记录错误
        if not self._auto_replies:
             logger.error("回复失败：未配置回复内容。")
             # 同上，让 perform_operation 处理
             pass

        # --- 添加随机延迟 (仅针对定时任务) ---
        is_manual = self._manual_trigger_reply
        trigger_type = "手动触发" if is_manual else "定时触发"
        if not is_manual:
            delay = random.uniform(5, 300) # 5到300秒随机延迟
            logger.info(f"定时任务触发 (回复)，随机延迟 {delay:.2f} 秒后执行...")
            time.sleep(delay)
            
        # 检查今天是否已执行（仅针对非手动触发） - 这个检查放在循环外，只判断是否需要启动这一轮回复
        if not is_manual and self._is_already_done_today(history_key):
            logger.info("根据历史记录，今日已回复过，跳过本次执行")
            return

        # 重置手动触发标志 (在循环开始前重置)
        self._manual_trigger_reply = False
        
        # --- 循环执行核心操作 ---
        reply_count = self._reply_times if self._reply_times > 0 else 1
        logger.info(f"计划执行 {reply_count} 次回复 ({trigger_type})。")

        for i in range(reply_count):
            attempt_num = i + 1
            logger.info(f"--- 开始第 {attempt_num}/{reply_count} 次回复尝试 ({trigger_type}) ---")
            operation_result = {} # 单次操作结果
            reply_dict = { # 单次操作用于历史/通知的数据
                "date": datetime.now(tz=pytz.timezone(settings.TZ)).strftime('%Y-%m-%d %H:%M:%S'),
                "status": "未知",
                "message": "",
                "trigger": trigger_type,
                "tid": None,
                "reply_content": ""
            }

            try:
                # 调用 _perform_operation 执行单次回复
                # 注意：每次调用都会创建新的 session, 处理 Cookie, 代理, 年龄确认
                operation_result = self._perform_operation(
                    self._reply_internal,
                    reply_fid=self._reply_fid,
                    auto_replies=self._auto_replies
                )
                reply_dict.update(operation_result)

            except ValueError as ve: # 配置错误 (理论上 perform_operation 会处理，但也可能在这里捕获)
                 logger.error(f"第 {attempt_num} 次回复配置错误: {ve}")
                 reply_dict["status"] = "配置错误"
                 reply_dict["message"] = str(ve)
            except requests.exceptions.RequestException as req_err: # 网络错误
                logger.error(f"第 {attempt_num} 次回复请求失败: {req_err}", exc_info=True)
                reply_dict["status"] = "请求失败"
                reply_dict["message"] = f"网络错误: {req_err}"
                if isinstance(req_err, requests.exceptions.HTTPError) and req_err.response is not None:
                    reply_dict["message"] = f"HTTP {req_err.response.status_code}: {self._preprocess_xml_text(req_err.response.text)}"
            except Exception as e:
                logger.error(f"第 {attempt_num} 次回复过程中发生错误: {e}", exc_info=True)
                reply_dict["status"] = "执行出错"
                reply_dict["message"] = str(e)
            finally:
                # 保存本次尝试的历史记录
                reply_dict['trigger'] = trigger_type # 确保 trigger 总是设置
                reply_dict['date'] = datetime.now(tz=pytz.timezone(settings.TZ)).strftime('%Y-%m-%d %H:%M:%S') # 确保时间是最终时间
                self._save_history(history_key, reply_dict)
                # 发送本次尝试的通知
                if self._notify and reply_dict.get("status") != "跳过":
                    self._send_notification(reply_dict, task_type="回复")
                    
                logger.info(f"--- 第 {attempt_num}/{reply_count} 次回复尝试完成。状态: {reply_dict['status']}, 消息: {reply_dict['message']}")
                # 记录成功的日期 (只要有一次成功就算完成当天任务，防止 _is_already_done_today 误判)
                if reply_dict["status"] == "回复成功" and not is_manual:
                    self._save_last_done_date(history_key)

            # 回复之间的间隔 (如果不是最后一次)
            if i < reply_count - 1:
                interval = random.uniform(15, 35) # 15到35秒间隔
                logger.info(f"等待 {interval:.2f} 秒后进行下一次回复...")
                time.sleep(interval)

        logger.info("============= ST98 回复任务结束 ==============")

    def _reply_internal(self, session: requests.Session, base_url: str, reply_fid: int, auto_replies: list, **kwargs) -> dict:
        """ 实际执行单次回复的内部逻辑，包含历史检查和过滤 """
        result = {"status": "未知", "message": "", "tid": None, "reply_content": ""}
        tid_to_reply = None
        formhash = None

        # --- 1. 加载并过滤历史记录 --- 
        history_key = f"{self.plugin_config_prefix}回复_history"
        recent_history = self.get_data(history_key) or []
        cutoff_date = datetime.now(pytz.timezone(settings.TZ)) - timedelta(days=self._history_days)
        replied_tids_history = set()
        for record in recent_history:
            try:
                record_time_naive = datetime.strptime(record["date"], '%Y-%m-%d %H:%M:%S')
                record_time = pytz.timezone(settings.TZ).localize(record_time_naive)
                # 只考虑指定天数内已处理过的帖子的TID
                if record_time >= cutoff_date and record.get('status') in ['回复成功', '等待审核', '回复过快', '部分成功']: # 部分成功的记录也排除
                    tid_in_record = record.get('tid')
                    if tid_in_record:
                        replied_tids_history.add(str(tid_in_record))
            except (ValueError, KeyError, TypeError):
                pass # 忽略格式错误的记录
        logger.info(f"从历史记录加载了 {len(replied_tids_history)} 个最近已处理的 TID 用于过滤。")

        # --- 2. 获取板块页面并选择未回复的帖子 --- 
        forum_url = f"{base_url}/forum.php?mod=forumdisplay&fid={reply_fid}"
        try:
            logger.info(f"_reply_internal: 正在访问板块页面: fid={reply_fid}")
            r = self._request_session(session, 'get', forum_url, headers={'Referer': f"{base_url}/forum.php"})
            all_tids_on_page = re.findall(r"normalthread_(\d+)", r.text, re.MULTILINE | re.IGNORECASE)
            if not all_tids_on_page:
                logger.warning(f"在板块 fid={reply_fid} 未找到任何帖子ID")
                result["status"] = "跳过"
                result["message"] = f"板块 {reply_fid} 无帖子"
                return result
               
            # 过滤掉已回复的
            available_tids = [tid for tid in all_tids_on_page if tid not in replied_tids_history]
           
            if not available_tids:
                logger.info(f"板块 fid={reply_fid} 上的所有帖子最近都已回复过。")
                result["status"] = "跳过"
                result["message"] = "无新帖可回复"
                return result
               
            # 从可用帖子中随机选择一个
            tid_to_reply = random.choice(available_tids)
            result["tid"] = tid_to_reply
            logger.info(f"_reply_internal: 随机选择未回复帖子 tid = {tid_to_reply} 进行回复")

        except requests.exceptions.RequestException as e:
            raise Exception(f"_reply_internal: 访问板块页面失败: {e}")
        except Exception as e:
            raise Exception(f"_reply_internal: 处理板块页面响应失败: {e}")

        # --- 3. 访问帖子页面获取 formhash --- 
        thread_url = f"{base_url}/forum.php?mod=viewthread&tid={tid_to_reply}"
        logger.info(f"_reply_internal: 正在访问帖子页面: tid={tid_to_reply}")
        try:
            r = self._request_session(session, 'get', thread_url, headers={'Referer': forum_url})
            soup = BeautifulSoup(r.text, 'lxml')
            formhash_input = soup.find('input', {'name': 'formhash'})
            if not formhash_input or not formhash_input.get('value'):
                 # 尝试从其他地方查找 formhash, 比如 reply 按钮的 onclick 事件
                 onclick_scripts = soup.find_all(string=re.compile(r"fastpostvalidate\(.*'formhash':\s*'(\w+)'\\)"))
                 if onclick_scripts:
                     match = re.search(r"'formhash':\s*'(\w+)'", onclick_scripts[0])
                     if match:
                          formhash = match.group(1)
                          logger.info(f"_reply_internal: 从脚本中获取 formhash 成功: {formhash}")
                 if not formhash:
                     logger.error(f"_reply_internal: 未能找到 formhash input: {formhash_input}")
                     logger.error(f"_reply_internal: 未能从脚本找到 formhash, 页面内容 (前500字符): {r.text[:500]}")
                     raise Exception('在帖子 tid={} 未找到formhash'.format(tid_to_reply))
            else:
                 formhash = formhash_input['value']
                 logger.info(f"_reply_internal: 从 input 获取 formhash 成功: {formhash}")

        except requests.exceptions.RequestException as e:
            raise Exception("_reply_internal: 访问帖子页面失败: {}".format(e))
        except Exception as e:
            raise Exception("_reply_internal: 解析帖子页面失败: {}".format(e))

        # --- 4. 准备回复内容 --- 
        if not auto_replies:
            raise ValueError("回复内容列表为空") 
        message = random.choice(auto_replies)
        result["reply_content"] = message
        logger.info(f"_reply_internal: 选择回复内容: {message}")

        # --- 5. 提交回复 --- 
        reply_action_url = f"{base_url}/forum.php?mod=post&action=reply&fid={reply_fid}&tid={tid_to_reply}&extra=page%3D1&replysubmit=yes&infloat=yes&handlekey=fastpost&inajax=1"
        reply_data = {
            'message': message,
            'posttime': int(time.time()),
            'formhash': formhash,
            'usesig': '1',
            'subject': '',
        }
        logger.info(f"_reply_internal: 正在提交回复到 tid={tid_to_reply}")
        try:
            r = self._request_session(session, 'post', reply_action_url, data=reply_data, headers={'Referer': thread_url})
            response_text = r.text
            logger.debug(f"_reply_internal: 回复提交响应: {response_text[:200]}...")
            processed_text = self._preprocess_xml_text(response_text)
            logger.info(f"_reply_internal: 回复结果: {processed_text}")

            if '回复发布成功' in processed_text or '操作成功' in processed_text:
                result["status"] = "回复成功"
                result["message"] = processed_text
            elif '您两次发表间隔少于' in processed_text:
                result["status"] = "回复过快"
                result["message"] = processed_text
            elif '审核' in processed_text:
                result["status"] = "等待审核"
                result["message"] = processed_text
            elif '需要先登录' in processed_text:
                result["status"] = "失败"
                result["message"] = "Cookie无效或已过期"
            else:
                result["status"] = "失败"
                result["message"] = processed_text
        except requests.exceptions.RequestException as e:
            if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
                response_text_processed = self._preprocess_xml_text(e.response.text)
                error_msg = f"_reply_internal: 提交回复失败: HTTP {e.response.status_code} - {response_text_processed}"
                raise Exception(error_msg)
            else:
                raise Exception("_reply_internal: 提交回复网络请求失败: {}".format(e))
        except Exception as e:
            raise Exception("_reply_internal: 处理回复提交响应时失败: {}".format(e))

        return result

    # --- 辅助方法 ---

    def _save_history(self, key: str, data: dict):
        """ 保存历史记录 (签到或回复) """
        try:
            history = self.get_data(key) or []
            history.append(data)

            # 清理旧记录
            now = datetime.now(tz=pytz.timezone(settings.TZ))
            valid_history = []
            for record in history:
                try:
                    record_date = datetime.strptime(record["date"], '%Y-%m-%d %H:%M:%S').replace(tzinfo=pytz.timezone(settings.TZ))
                    if (now - record_date).days < self._history_days:
                        valid_history.append(record)
                except (ValueError, KeyError, TypeError):
                    logger.warning(f"历史记录日期格式无效或丢失: {record.get('date', 'N/A')}")
                    # 尝试添加当前时间并保留，或者直接丢弃旧格式记录
                    # 为了简单起见，这里直接丢弃格式错误的记录
                    pass

            self.save_data(key=key, value=valid_history)
            logger.debug(f"保存 {key} 历史记录，当前共有 {len(valid_history)} 条记录")
        except Exception as e:
            logger.error(f"保存 {key} 历史记录失败: {e}", exc_info=True)

    def _is_manual_trigger(self, task_type: str) -> bool:
        """ 检查是否为手动触发 """
        if task_type == "sign":
            return self._manual_trigger_sign
        elif task_type == "reply":
            return self._manual_trigger_reply
        return False

    def _is_already_done_today(self, key: str) -> bool:
        """ 检查今天是否已经成功执行过 (签到或回复) """
        today = datetime.now(tz=pytz.timezone(settings.TZ)).strftime('%Y-%m-%d')
        history = self.get_data(key) or []
        for record in reversed(history): # 从后往前检查更快
            if record.get("date", "").startswith(today):
                 # 签到成功/已签到 或 回复成功 视为完成
                 if record.get("status") in ["签到成功", "已签到", "回复成功", "跳过"]: # 跳过也算完成，避免重复执行
                      logger.info(f"今日 {key} 已完成，最后状态: {record['status']}")
                      return True
                 elif record.get("status") in ["失败", "请求失败", "执行出错", "配置错误"]:
                     # 如果今天最后一次执行失败，则允许重试
                     logger.info(f"今日 {key} 最后一次执行状态为 {record['status']}，允许重试")
                     return False
        # 如果今天没有记录，则认为未完成
        logger.info(f"今日无 {key} 执行记录")
        return False

    def _save_last_done_date(self, key: str):
        """ 保存最后一次成功执行的日期时间 """
        now_str = datetime.now(tz=pytz.timezone(settings.TZ)).strftime('%Y-%m-%d %H:%M:%S')
        self.save_data(f"last_{key}_date", now_str)
        logger.debug(f"记录 {key} 最后成功时间: {now_str}")

    def _send_notification(self, result_dict: dict, task_type: str):
        """ 发送通知 """
        if not self._notify:
            return

        status = result_dict.get("status", "未知")
        raw_message = result_dict.get("message", "")
        reward_amount = result_dict.get("reward_amount")
        if not reward_amount and status == "签到成功":
             reward_match = re.search(r'(?:获得|奖励)\s*(\d+)\s*金钱', raw_message)
             if reward_match:
                 reward_amount = reward_match.group(1)

        # 用户信息
        username = result_dict.get("username")
        points_before = result_dict.get("points_before")
        money_before = result_dict.get("money_before")

        # 清理消息文本
        if status == "签到成功":
            message = f"获得 {reward_amount if reward_amount is not None else '未知'} 金钱"
        elif status == "已签到":
            message = "已经签到过了"
        elif status == "回复成功":
            message = "回复发布成功"
        else:
            message = re.sub(r'<[^>]+>', ' ', raw_message)
            message = re.sub(r'\\s+', ' ', message).strip()
            if len(message) > 50: message = message[:47] + "..."

        trigger = result_dict.get("trigger", "未知触发")
        timestamp = result_dict.get("date", datetime.now(tz=pytz.timezone(settings.TZ)).strftime('%Y-%m-%d %H:%M:%S'))
        tid = result_dict.get("tid")

        title_prefix = "✅" if status in ["签到成功", "已签到", "回复成功"] else \
                       "❌" if status in ["失败", "请求失败", "执行出错", "配置错误"] else \
                       "ℹ️" if status == "跳过" else "⚠️"

        title = f"【{title_prefix} ST98 {task_type} {status}】"

        text = f"📢 执行结果\n━━━━━━━━━━\n"
        if task_type == "签到":
            text += f"👤 用户：{username if username else '-'}\n"
            text += f"💰 金钱：{money_before if money_before is not None else '-'} (+{reward_amount if reward_amount is not None else '0'})\n" # 显示签到前和奖励
            text += f"💎 积分：{points_before if points_before is not None else '-'}\n"

        text += (
            f"🕐 时间：{timestamp}\n"
            f"✨ 状态：{status}\n"
            f"💬 消息：{message}\n"
            f"📍 方式：{trigger}\n"
        )

        if task_type == "回复" and tid:
             text += f"🔗 帖子 TID：{tid}\n"

        text += f"━━━━━━━━━━"

        # 发送通知
        try:
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title=title,
                text=text
            )
            logger.debug("发送通知成功")
        except Exception as e:
            logger.error(f"发送通知失败: {e}", exc_info=True)

    # --- 标准插件方法 ---

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        services = []
        if not self._enabled:
            return services

        # 签到定时任务
        if self._sign_cron:
            logger.info(f"注册 ST98 签到定时服务: {self._sign_cron}")
            services.append({
                "id": "st98sign_sign",
                "name": "ST98签到",
                "trigger": CronTrigger.from_crontab(self._sign_cron, timezone=settings.TZ),
                "func": self.sign,
                "kwargs": {}
            })

        # 回复定时任务
        if self._reply_cron:
            logger.info(f"注册 ST98 回复定时服务: {self._reply_cron}")
            services.append({
                "id": "st98sign_reply",
                "name": "ST98回复",
                "trigger": CronTrigger.from_crontab(self._reply_cron, timezone=settings.TZ),
                "func": self.reply,
                "kwargs": {}
            })

        return services

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """ 获取插件配置表单 """
        logger.debug("开始执行 get_form (st98sign)")
        st98sign.plugin_has_page = True
        setattr(self, "_has_page", True)
        default_replies_text = "\n".join(self.DEFAULT_REPLIES)

        form_config = [
            {
                'component': 'VForm',
                'content': [
                    # --- 鸣谢 Alert (顶部) ---
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol', 'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'success', 'variant': 'tonal',
                                            'text': '特别鸣谢：感谢 Desire.大佬 鼎力支持！',
                                            'density': 'compact', 'class': 'mb-4'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # --- 使用说明 Alert (移除) ---
                    # {
                    #     'component': 'VRow',
                    #     'content': [
                    #         {
                    #             'component': 'VCol', 'props': {'cols': 12},
                    #             'content': [
                    #                 {
                    #                     'component': 'VAlert',
                    #                     'props': {
                    #                         'type': 'info', 'variant': 'tonal',
                    #                         'text': '【使用说明】... ', # 原说明内容
                    #                         'class': 'mb-4'
                    #                     }
                    #                 }
                    #             ]
                    #         }
                    #     ]
                    # },
                    # --- 卡片 1: 通用设置 ---
                    {
                        'component': 'VCard',
                        'props': {'variant': 'outlined', 'class': 'mb-4'},
                        'content': [
                            {'component': 'VCardTitle', 'props': {'class': 'text-h6'}, 'text': '⚙️ 通用设置'},
                            {'component': 'VCardText', 'content': [
                                # 开关行 (启用, 通知)
                                {'component': 'VRow', 'content': [
                                    {'component': 'VCol', 'props': {'cols': 12, 'sm': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'sm': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '开启通知'}}]},
                                ]},
                                # Cookie 行
                                {'component': 'VRow', 'content': [
                                    {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VTextField', 'props': {'model': 'cookie', 'label': '站点Cookie', 'placeholder': '在此粘贴', 'hint': '有效期有限，需定期更新'}}]},
                                ]},
                                # Host, Proxy, History Days 行
                                {'component': 'VRow', 'content': [
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {'model': 'host', 'label': '站点Host', 'placeholder': 't66y.com', 'hint': '站点域名 (不需要https://)'}}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {'model': 'proxy', 'label': '代理服务器 (可选)', 'placeholder': 'http://127.0.0.1:7890', 'hint': '支持 http/https/socks5'}}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {'model': 'history_days', 'label': '历史过滤天数', 'type': 'number', 'placeholder': '30', 'hint': '页面显示和回复过滤的历史天数'}}]}, # Hint 已存在
                                ]}
                            ]}
                        ]
                    },
                    # --- 卡片 2: 签到设置 ---
                    {
                        'component': 'VCard',
                        'props': {'variant': 'outlined', 'class': 'mb-4'},
                        'content': [
                            {'component': 'VCardTitle', 'props': {'class': 'text-h6'}, 'text': '📊 签到设置'},
                            {'component': 'VCardText', 'content': [
                                {'component': 'VRow', 'content': [
                                    {'component': 'VCol', 'props': {'cols': 12, 'sm': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'sign_onlyonce', 'label': '立即签到一次'}}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'sm': 6}, 'content': [{'component': 'VCronField', 'props': {'model': 'sign_cron', 'label': '签到周期 Cron', 'hint': '留空则禁用。预设值: 0 8 * * *'}}]},
                                ]}
                            ]}
                        ]
                    },
                    # --- 卡片 3: 回复设置 ---
                    {
                        'component': 'VCard',
                        'props': {'variant': 'outlined', 'class': 'mb-4'},
                        'content': [
                            {'component': 'VCardTitle', 'props': {'class': 'text-h6'}, 'text': '💬 回复设置'},
                            {'component': 'VCardText', 'content': [
                                # 立即回复和 Cron 行
                                {'component': 'VRow', 'content': [
                                    {'component': 'VCol', 'props': {'cols': 12, 'sm': 6}, 'content': [{'component': 'VSwitch', 'props': {'model': 'reply_onlyonce', 'label': '立即回复一次'}}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'sm': 6}, 'content': [{'component': 'VCronField', 'props': {'model': 'reply_cron', 'label': '回复周期 Cron', 'hint': '留空则禁用。预设值: 0 10,18 * * *'}}]},
                                ]},
                                # FID 和 次数 行
                                {'component': 'VRow', 'content': [
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'reply_fid', 'label': '回复板块FID', 'type': 'number', 'placeholder': '103', 'hint': '目标板块数字ID，可在论坛URL中查看'}}]},
                                    {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'reply_times', 'label': '每次回复数量', 'type': 'number', 'placeholder': '1', 'hint': '每次任务尝试回复多少个帖子'}}]},
                                ]},
                                # 回复内容 行
                                {'component': 'VRow', 'content': [
                                    {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VTextarea', 'props': {'model': 'auto_replies', 'label': '自动回复内容 (每行一条)', 'placeholder': '留空则使用默认回复', 'rows': 6, 'hint': '系统会随机选择其中一条作为回复'}}]},
                                ]}
                            ]}
                        ]
                    },
                ]
            }
        ]
        default_values = {
            "enabled": False, "notify": True,
            "cookie": "", "host": "", "proxy": "", "history_days": 30,
            "sign_onlyonce": False, "sign_cron": "0 8 * * *",
            "reply_onlyonce": False, "reply_cron": "0 10,18 * * *", "reply_fid": 103, "reply_times": 1, "auto_replies": default_replies_text,
        }
        logger.debug("完成执行 get_form (st98sign)")
        return form_config, default_values

    def get_page(self) -> List[dict]:
        """
        构建插件详情页面，分为三个卡片：汇总信息、签到历史、回复历史
        """
        sign_history_key = f"{self.plugin_config_prefix}签到_history"
        reply_history_key = f"{self.plugin_config_prefix}回复_history"
        sign_history_raw = self.get_data(sign_history_key) or []
        reply_history_raw = self.get_data(reply_history_key) or []

        # 确保历史记录按日期倒序排列
        sign_history = sorted(sign_history_raw, key=lambda x: x.get("date", ""), reverse=True)
        reply_history = sorted(reply_history_raw, key=lambda x: x.get("date", ""), reverse=True)

        page_components = [] # 初始化页面组件列表

        # --- 1. 构建汇总卡片 --- 
        latest_sign_record = sign_history[0] if sign_history else {}
        username = latest_sign_record.get('username', '未知')
        money_before = latest_sign_record.get('money_before', '未知')
        points_before = latest_sign_record.get('points_before', '未知')

        last_success_sign_time = '从未成功'
        for record in sign_history: # 已经倒序，直接遍历
            if record.get('status') in ['签到成功', '已签到']:
                last_success_sign_time = record.get('date', '未知')
                break

        today_str = datetime.now(tz=pytz.timezone(settings.TZ)).strftime('%Y-%m-%d')
        today_sign_status = '未执行'
        today_sign_msg = ''
        today_sign_status_color = 'grey'
        for record in sign_history: # 已经倒序，直接遍历查找当天的第一条记录
            if record.get('date', '').startswith(today_str):
                today_sign_status = record.get('status', '未知')
                today_sign_msg = record.get('message', '')
                if today_sign_status in ['签到成功', '已签到']:
                    today_sign_status_color = 'success'
                elif today_sign_status in ['失败', '请求失败', '执行出错', '配置错误']:
                    today_sign_status_color = 'error'
                else:
                    today_sign_status_color = 'warning' # 其他状态如等待审核
                break

        summary_card = {
            'component': 'VCard',
            'props': {'variant': 'outlined', 'class': 'mb-4'},
            'content': [
                {'component': 'VCardTitle', 'props': {'class': 'text-h6'}, 'text': '📊 当前状态'},
                {'component': 'VCardText', 'content': [
                    {'component': 'VRow', 'props': {'dense': True}, 'content': [
                        # 用户名
                        {'component': 'VCol', 'props': {'cols': 12, 'sm': 6, 'md': 3}, 'content': [
                            {'component': 'div', 'props': {'class': 'd-flex align-center'},
                             'content': [
                                 {'component': 'VIcon', 'props': {'start': True, 'icon': 'mdi-account', 'color': 'primary'}},
                                 {'component': 'span', 'text': f"用户: {username}"}
                             ]}
                        ]},
                        # 金钱
                        {'component': 'VCol', 'props': {'cols': 12, 'sm': 6, 'md': 3}, 'content': [
                            {'component': 'div', 'props': {'class': 'd-flex align-center'},
                             'content': [
                                 {'component': 'VIcon', 'props': {'start': True, 'icon': 'mdi-cash', 'color': 'orange'}},
                                 {'component': 'span', 'text': f"金钱(上次获取): {money_before}"}
                             ]}
                        ]},
                        # 积分
                        {'component': 'VCol', 'props': {'cols': 12, 'sm': 6, 'md': 3}, 'content': [
                            {'component': 'div', 'props': {'class': 'd-flex align-center'},
                             'content': [
                                 {'component': 'VIcon', 'props': {'start': True, 'icon': 'mdi-star-four-points', 'color': 'purple'}},
                                 {'component': 'span', 'text': f"积分(上次获取): {points_before}"}
                             ]}
                        ]},
                        # 今日签到状态
                        {'component': 'VCol', 'props': {'cols': 12, 'sm': 6, 'md': 3}, 'content': [
                            {'component': 'div', 'props': {'class': 'd-flex align-center'},
                             'content': [
                                 {'component': 'VIcon', 'props': {'start': True, 'icon': 'mdi-calendar-check', 'color': today_sign_status_color}},
                                 {'component': 'span', 'text': '今日签到: ', 'class': 'mr-1'},
                                 {'component': 'VChip', 'props': {'color': today_sign_status_color, 'size': 'small', 'variant': 'tonal'}, 'text': today_sign_status}
                             ]}
                        ]},
                        # 如果今日状态不是未执行，显示具体消息
                        *([{'component': 'VCol', 'props': {'cols': 12}, 'content': [
                             {'component': 'div', 'props': {'class': 'd-flex align-center text-caption text-grey'},
                              'content': [
                                 {'component': 'VIcon', 'props': {'start': True, 'icon': 'mdi-message-outline', 'size': 'small'}},
                                 {'component': 'span', 'text': f"今日消息: {today_sign_msg if today_sign_msg else '-'}"}
                             ]}
                        ]}] if today_sign_status != '未执行' else []),
                        # 上次成功签到
                        {'component': 'VCol', 'props': {'cols': 12}, 'content': [
                             {'component': 'div', 'props': {'class': 'd-flex align-center text-caption text-grey'},
                              'content': [
                                 {'component': 'VIcon', 'props': {'start': True, 'icon': 'mdi-history', 'size': 'small'}},
                                 {'component': 'span', 'text': f"上次成功签到时间: {last_success_sign_time}"}
                             ]}
                        ]},
                    ]}
                ]}
            ]
        }
        page_components.append(summary_card)

        # --- 2. 构建签到历史卡片 --- 
        if sign_history:
            sign_history_rows = []
            for history in sign_history: # 已倒序
                status_text = history.get("status", "未知")
                status_color = "success" if status_text in ["签到成功", "已签到"] else \
                               "error" if status_text in ["失败", "请求失败", "执行出错", "配置错误"] else \
                               "info" if status_text == "跳过" else "warning"
                full_message = history.get('message', '-')
                display_message = (full_message[:25] + '...') if len(full_message) > 28 else full_message

                sign_history_rows.append({
                    'component': 'tr',
                    'content': [
                        {'component': 'td', 'props': {'class': 'text-caption'}, 'text': history.get("date", "-")},
                        {'component': 'td', 'text': history.get('username', '-')},
                        {'component': 'td', 'content': [{'component': 'VChip', 'props': {'color': status_color, 'size': 'small', 'variant': 'outlined'}, 'text': status_text}]},
                        {'component': 'td', 'text': display_message, 'attrs': {'title': full_message}},
                        {
                            'component': 'td',
                            'text': f"{history.get('money_before', '?')} (+{history.get('reward_amount', '?') if history.get('status') == '签到成功' else '-'})"
                        },
                        {'component': 'td', 'text': str(history.get('points_before', '?'))},
                        {'component': 'td', 'props': {'class': 'text-caption'}, 'text': history.get("trigger", "-")}
                    ]
                })
            
            sign_table = {
                'component': 'VTable',
                'props': {'hover': True, 'density': 'compact'},
                'content': [
                    {'component': 'thead', 'content': [{
                        'component': 'tr',
                        'content': [
                            {'component': 'th', 'text': '时间'},
                            {'component': 'th', 'text': '用户名'},
                            {'component': 'th', 'text': '状态'},
                            {'component': 'th', 'text': '消息'},
                            {'component': 'th', 'text': '金钱(前+奖励)'},
                            {'component': 'th', 'text': '积分(前)'},
                            {'component': 'th', 'text': '触发方式'}
                        ]
                    }]},
                    {'component': 'tbody', 'content': sign_history_rows}
                ]
            }

            sign_history_card = {
                'component': 'VCard',
                'props': {'variant': 'outlined', 'class': 'mb-4'},
                'content': [
                    # 卡片标题
                    {'component': 'VCardTitle',
                     'text': '📅 签到历史',
                     'content': [
                         {'component': 'VIcon', 'props': {'start': True, 'icon': 'mdi-clipboard-text-clock-outline', 'color': 'blue-grey'}}
                     ]},
                    # 折叠面板容器
                    {'component': 'VExpansionPanels', 
                     'props': {'class': 'ma-0 pa-0'}, # 移除手风琴模式，允许同时展开；去除内外边距
                     'content': [
                         # 单个折叠面板
                         {'component': 'VExpansionPanel',
                          'content': [
                              # 折叠面板的标题 (可点击)
                              {'component': 'VExpansionPanelTitle', 'text': '点击展开/隐藏记录'},
                              # 折叠面板的内容
                              {'component': 'VExpansionPanelText', 'props': {'class': 'pa-0'}, # 移除内边距
                               'content': [sign_table] # 表格放在折叠内容里
                              }
                          ]}
                     ]}
                ]
            }
            page_components.append(sign_history_card) # <-- 修正：添加签到历史卡片
        else:
            # 如果没有签到历史，显示提示信息
            page_components.append({
                'component': 'VAlert',
                'props': {
                    'type': 'info', 'variant': 'tonal',
                    'text': '暂无签到记录', 'class': 'mb-4'
                }
            })

        # --- 3. 构建回复历史卡片 --- 
        if reply_history:
            reply_history_rows = []
            for history in reply_history: # 已倒序
                status_text = history.get("status", "未知")
                status_color = "success" if status_text == "回复成功" else \
                               "error" if status_text in ["失败", "请求失败", "执行出错", "配置错误"] else \
                               "info" if status_text == "跳过" else "warning"
                full_status_message = history.get('message', '-')
                display_status_message = (full_status_message[:20] + '...') if len(full_status_message) > 23 else full_status_message
                full_reply_content = history.get('reply_content', '-')
                display_reply_content = (full_reply_content[:20] + '...') if len(full_reply_content) > 23 else full_reply_content
                
                reply_history_rows.append({
                    'component': 'tr',
                    'content': [
                        {'component': 'td', 'props': {'class': 'text-caption'}, 'text': history.get("date", "-")},
                        {'component': 'td', 'content': [{'component': 'VChip', 'props': {'color': status_color, 'size': 'small', 'variant': 'outlined'}, 'text': status_text}]},
                        {'component': 'td', 'text': display_status_message, 'attrs': {'title': full_status_message}},
                        {'component': 'td', 'props': {'class': 'text-caption'}, 'text': history.get("trigger", "-")},
                        {'component': 'td', 'text': history.get('tid', '-')},
                        {'component': 'td', 'text': display_reply_content, 'attrs': {'title': full_reply_content}}
                    ]
                })

            reply_table = {
                'component': 'VTable',
                'props': {'hover': True, 'density': 'compact'},
                'content': [
                    {'component': 'thead', 'content': [{
                        'component': 'tr',
                        'content': [
                            {'component': 'th', 'text': '时间'},
                            {'component': 'th', 'text': '状态'},
                            {'component': 'th', 'text': '消息'},
                            {'component': 'th', 'text': '触发方式'},
                            {'component': 'th', 'text': 'TID'},
                            {'component': 'th', 'text': '回复内容'}
                        ]
                    }]},
                    {'component': 'tbody', 'content': reply_history_rows}
                ]
            }

            reply_history_card = {
                'component': 'VCard',
                'props': {'variant': 'outlined', 'class': 'mb-4'},
                'content': [
                     # 卡片标题
                    {'component': 'VCardTitle',
                     'text': '💬 回复历史', 
                     'content': [
                         {'component': 'VIcon', 'props': {'start': True, 'icon': 'mdi-message-reply-text-outline', 'color': 'teal'}}
                     ]},
                     # 折叠面板容器 (与签到历史保持一致)
                    {'component': 'VExpansionPanels', 
                     'props': {'class': 'ma-0 pa-0'}, # 移除 accordion, 去除内外边距
                     'content': [
                         # 单个折叠面板
                         {'component': 'VExpansionPanel',
                          'content': [
                              # 折叠面板的标题 (可点击)
                              {'component': 'VExpansionPanelTitle', 'text': '点击展开/隐藏记录'}, # <-- 修复：添加标题
                              # 折叠面板的内容
                              {'component': 'VExpansionPanelText', 'props': {'class': 'pa-0'}, # 移除内边距
                               'content': [reply_table] # 表格放在折叠内容里
                              }
                          ]}
                     ]}
                ]
            }
            page_components.append(reply_history_card) # <-- 修正：添加回复历史卡片
        else:
            # 如果没有回复历史，显示提示信息
            page_components.append({
                'component': 'VAlert',
                'props': {
                    'type': 'info', 'variant': 'tonal',
                    'text': '暂无回复记录', 'class': 'mb-4'
                }
            })

        # 如果完全没有历史记录 (也处理一下，虽然前面有检查，但更保险)
        if not sign_history and not reply_history:
             return [
                 {
                     'component': 'VAlert',
                     'props': {
                         'type': 'info', 'variant': 'tonal',
                         'text': '暂无任何签到或回复记录', 'class': 'mb-2'
                     }
                 }
             ]

        logger.debug(f"[get_page] Returning {len(page_components)} components.") # 诊断日志
        return page_components

    def stop_service(self):
        """ 停止服务，清理所有任务 """
        try:
            if self._scheduler_sign:
                if self._scheduler_sign.running:
                    self._scheduler_sign.shutdown(wait=False)
                self._scheduler_sign = None
            if self._scheduler_reply:
                if self._scheduler_reply.running:
                    self._scheduler_reply.shutdown(wait=False)
                self._scheduler_reply = None
            logger.info("ST98 签到回复插件服务已停止")
        except Exception as e:
            logger.error(f"停止 ST98 签到回复 插件服务失败: {str(e)}")

    def get_command(self) -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "detail",
                "name": "查看详情",
                "category": "插件",
                "desc": "查看签到回复历史记录",
                "function": "get_history_html",
            }
        ]

    def get_history_html(self, _):
        """获取历史记录的HTML表示形式"""
        sign_history_key = f"{self.plugin_config_prefix}签到_history"
        reply_history_key = f"{self.plugin_config_prefix}回复_history"
        sign_history = self.get_data(sign_history_key) or []
        reply_history = self.get_data(reply_history_key) or []

        # 构建HTML输出
        html = "<h4>📊 签到历史记录</h4>"
        if not sign_history:
            html += "<p>暂无签到记录</p>"
        else:
            html += "<table border='1' cellpadding='5' style='border-collapse:collapse'>"
            html += "<tr><th>时间</th><th>用户名</th><th>状态</th><th>消息</th><th>金钱(前+奖励)</th><th>积分(前)</th><th>触发方式</th></tr>"
            for record in sorted(sign_history, key=lambda x: x.get("date", ""), reverse=True):
                html += f"<tr>"
                html += f"<td>{record.get('date', '-')}</td>"
                html += f"<td>{record.get('username', '-')}</td>"
                status = record.get('status', '未知')
                status_color = 'green' if status in ['签到成功', '已签到'] else ('blue' if status == '跳过' else ('orange' if status in ['回复过快', '等待审核'] else 'red'))
                html += f"<td style='color:{status_color}'>{status}</td>"
                html += f"<td>{record.get('message', '-')}</td>"
                money_before = record.get('money_before', '?')
                reward = record.get('reward_amount')
                reward_str = f" (+{reward if reward is not None else '?'})" if status == '签到成功' else ""
                html += f"<td>{money_before}{reward_str}</td>"
                points_before = record.get('points_before', '?')
                html += f"<td>{points_before}</td>"
                html += f"<td>{record.get('trigger', '-')}</td>"
                html += "</tr>"
            html += "</table>"
        
        html += "<h4>📊 回复历史记录</h4>"
        if not reply_history:
            html += "<p>暂无回复记录</p>"
        else:
            html += "<table border='1' cellpadding='5' style='border-collapse:collapse'>"
            html += "<tr><th>时间</th><th>状态</th><th>消息</th><th>触发方式</th><th>TID</th><th>回复内容</th></tr>"
            for record in sorted(reply_history, key=lambda x: x.get("date", ""), reverse=True):
                html += f"<tr>"
                html += f"<td>{record.get('date', '-')}</td>"
                status = record.get('status', '未知')
                status_color = 'green' if status == '回复成功' else ('blue' if status == '跳过' else ('orange' if status in ['回复过快', '等待审核'] else 'red'))
                html += f"<td style='color:{status_color}'>{status}</td>"
                html += f"<td>{record.get('message', '-')}</td>"
                html += f"<td>{record.get('trigger', '-')}</td>"
                html += f"<td>{record.get('tid', '-')}</td>"
                html += f"<td>{record.get('reply_content', '-')}</td>"
                html += "</tr>"
            html += "</table>"
            
        return html

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件API定义"""
        return [
            {
                "path": "/do_sign",
                "method": "GET",
                "summary": "执行签到",
                "description": "立即执行一次签到",
                "function": self.sign
            },
            {
                "path": "/do_reply",
                "method": "GET",
                "summary": "执行回复",
                "description": "立即执行一次回复",
                "function": self.reply
            }
        ] 

    # --- 辅助方法 ---
    def _get_user_info(self, session: requests.Session, base_url: str, **kwargs) -> dict:
        """ 获取用户名、积分和金钱信息 """
        user_info = {"username": None, "points": None, "money": None}
        credit_url = f"{base_url}/home.php?mod=spacecp&ac=credit"
        logger.info(f"_get_user_info: 正在访问积分页面: {credit_url}")
        try:
            r = self._request_session(session, 'get', credit_url, headers={'Referer': base_url + '/'})
            soup = BeautifulSoup(r.text, 'lxml')

            # 提取用户名
            username_tag = soup.select_one('strong.vwmy a')
            if username_tag:
                user_info["username"] = username_tag.text.strip()
                logger.debug(f"提取到用户名: {user_info['username']}")
            else:
                logger.warning("未能从页面提取用户名")

            # 提取积分 (方法一：通过特定链接)
            points_tag = soup.select_one('a#extcreditmenu')
            if points_tag:
                points_text = points_tag.text.strip()
                points_match = re.search(r'积分:\s*(\d+)', points_text)
                if points_match:
                    try:
                        user_info["points"] = int(points_match.group(1))
                        logger.debug(f"提取到积分: {user_info['points']}")
                    except ValueError:
                        logger.warning(f"无法将提取到的积分文本 '{points_match.group(1)}' 转换为数字")
                else:
                    logger.warning(f"未能从积分链接文本 '{points_text}' 中解析出数字")
            else:
                 # 提取积分 (方法二：通过 creditl 列表 - 作为备用)
                 credit_items = soup.select('ul.creditl li')
                 for item in credit_items:
                     if '积分:' in item.text and '总积分' not in item.text: # 避免匹配到总积分行
                         points_match = re.search(r'积分:\s*(\d+)', item.text)
                         if points_match:
                              try:
                                 user_info["points"] = int(points_match.group(1))
                                 logger.debug(f"提取到积分 (备用方法): {user_info['points']}")
                                 break # 找到就跳出
                              except ValueError:
                                   logger.warning(f"无法将提取到的积分文本 (备用方法) '{points_match.group(1)}' 转换为数字")
                 if user_info["points"] is None:
                      logger.warning("未能从页面提取积分")

            # 提取金钱
            money_tag = soup.select_one('ul.creditl li.xi1') # 选择包含"金钱:"的那个 li
            if money_tag:
                money_text = money_tag.text.strip()
                money_match = re.search(r'金钱:\s*(\d+)', money_text)
                if money_match:
                    try:
                        user_info["money"] = int(money_match.group(1))
                        logger.debug(f"提取到金钱: {user_info['money']}")
                    except ValueError:
                        logger.warning(f"无法将提取到的金钱文本 '{money_match.group(1)}' 转换为数字")
                else:
                    logger.warning(f"未能从金钱标签文本 '{money_text}' 中解析出数字")
            else:
                logger.warning("未能从页面提取金钱")

        except requests.exceptions.RequestException as e:
            logger.error(f"访问积分页面失败: {e}", exc_info=True)
            # 这里不抛出异常，允许签到继续，但会缺少用户信息
        except Exception as e:
            logger.error(f"解析积分页面时出错: {e}", exc_info=True)
            # 同上

        return user_info

    def _save_history(self, key: str, data: dict):
        """ 保存历史记录 (签到或回复) """
        try:
            history = self.get_data(key) or []
            history.append(data)

            # 清理旧记录
            now = datetime.now(tz=pytz.timezone(settings.TZ))
            valid_history = []
            for record in history:
                try:
                    record_date = datetime.strptime(record["date"], '%Y-%m-%d %H:%M:%S').replace(tzinfo=pytz.timezone(settings.TZ))
                    if (now - record_date).days < self._history_days:
                        valid_history.append(record)
                except (ValueError, KeyError, TypeError):
                    logger.warning(f"历史记录日期格式无效或丢失: {record.get('date', 'N/A')}")
                    # 尝试添加当前时间并保留，或者直接丢弃旧格式记录
                    # 为了简单起见，这里直接丢弃格式错误的记录
                    pass

            self.save_data(key=key, value=valid_history)
            logger.debug(f"保存 {key} 历史记录，当前共有 {len(valid_history)} 条记录")
        except Exception as e:
            logger.error(f"保存 {key} 历史记录失败: {e}", exc_info=True)