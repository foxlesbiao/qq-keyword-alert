from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

DIGITS_RE = re.compile(r"^\d+$")
DEFAULT_RULES = {}
DEFAULT_NOTIFY_QQ = ""


@register("qq-keyword-alert", "OpenClaw", "监控指定QQ群关键词，命中后私聊管理员通知", "1.1.0")
class QQKeywordAlert(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or AstrBotConfig()
        self.last_hits: dict[str, float] = {}
        self._load_config()

    def _normalize_list(self, values: list[Any]) -> list[str]:
        seen = set()
        result = []
        for value in values:
            item = str(value).strip()
            if not item or item in seen:
                continue
            seen.add(item)
            result.append(item)
        return result

    def _migrate_legacy_rules(self) -> dict[str, dict[str, Any]]:
        rules = self.config.get("rules")
        if isinstance(rules, dict) and rules:
            return rules
        watch_groups = self._normalize_list(self.config.get("watch_groups", []))
        keywords = self._normalize_list(self.config.get("keywords", []))
        exclude_keywords = self._normalize_list(self.config.get("exclude_keywords", []))
        migrated = {}
        for gid in watch_groups:
            migrated[gid] = {
                "keywords": keywords[:],
                "exclude_keywords": exclude_keywords[:],
                "enabled": True,
                "regex_mode": False,
            }
        return migrated

    def _load_config(self):
        self.rules: dict[str, dict[str, Any]] = {}
        raw_rules = self._migrate_legacy_rules()
        for group_id, rule in raw_rules.items():
            gid = str(group_id).strip()
            if not gid:
                continue
            self.rules[gid] = {
                "keywords": self._normalize_list(rule.get("keywords", [])),
                "exclude_keywords": self._normalize_list(rule.get("exclude_keywords", [])),
                "enabled": bool(rule.get("enabled", True)),
                "regex_mode": bool(rule.get("regex_mode", False)),
            }
        self.notify_user_id = str(self.config.get("notify_user_id", DEFAULT_NOTIFY_QQ)).strip()
        self.case_sensitive = bool(self.config.get("case_sensitive", False))
        self.cooldown_seconds = int(self.config.get("cooldown_seconds", 30))

    def _save(self):
        try:
            self.config["rules"] = self.rules
            self.config["notify_user_id"] = self.notify_user_id
            self.config["case_sensitive"] = self.case_sensitive
            self.config["cooldown_seconds"] = self.cooldown_seconds
            self.config.save_config()
            self._load_config()
            return True, ""
        except Exception as e:
            logger.error(f"qq-keyword-alert: 保存配置失败: {e}")
            return False, str(e)

    def _normalize(self, text: str) -> str:
        return text if self.case_sensitive else text.lower()

    def _highlight_hits(self, text: str, matched: list[str]) -> str:
        out = text
        for kw in sorted(set(matched), key=len, reverse=True):
            if kw:
                out = out.replace(kw, f"【{kw}】")
        return out

    def _is_private_chat(self, event: AstrMessageEvent) -> bool:
        return not bool(event.get_group_id())

    async def _reply(self, event: AstrMessageEvent, text: str):
        await event.send(MessageChain([Plain(text)]))

    async def _require_private_admin(self, event: AstrMessageEvent) -> bool:
        if not self._is_private_chat(event):
            await self._reply(event, "仅允许管理员私聊操作。")
            return False
        return True

    def _valid_digits(self, value: str) -> bool:
        return bool(value and DIGITS_RE.fullmatch(value))

    def _get_rule(self, group_id: str) -> dict[str, Any] | None:
        return self.rules.get(group_id)

    def _ensure_rule(self, group_id: str) -> dict[str, Any]:
        if group_id not in self.rules:
            self.rules[group_id] = {
                "keywords": [],
                "exclude_keywords": [],
                "enabled": True,
                "regex_mode": False,
            }
        return self.rules[group_id]

    def _match_keywords(self, text: str, group_id: str) -> list[str]:
        rule = self._get_rule(group_id)
        if not rule:
            return []
        keywords = rule.get("keywords", [])
        if rule.get("regex_mode"):
            matched = []
            for kw in keywords:
                try:
                    if re.search(kw, text, 0 if self.case_sensitive else re.IGNORECASE):
                        matched.append(kw)
                except re.error as e:
                    logger.error(f"qq-keyword-alert: regex 错误 keyword={kw}: {e}")
            return matched
        base = self._normalize(text)
        return [kw for kw in keywords if self._normalize(kw) in base]

    def _has_exclude(self, text: str, group_id: str) -> bool:
        rule = self._get_rule(group_id)
        if not rule:
            return False
        excludes = rule.get("exclude_keywords", [])
        if rule.get("regex_mode"):
            for kw in excludes:
                try:
                    if re.search(kw, text, 0 if self.case_sensitive else re.IGNORECASE):
                        return True
                except re.error as e:
                    logger.error(f"qq-keyword-alert: regex 错误 exclude={kw}: {e}")
            return False
        base = self._normalize(text)
        return any(self._normalize(kw) in base for kw in excludes)

    def _dedupe_key(self, group_id: str, sender_id: str, text: str) -> str:
        return f"{group_id}:{sender_id}:{text.strip()}"

    def _in_cooldown(self, key: str, now_ts: float) -> bool:
        last_ts = self.last_hits.get(key)
        return last_ts is not None and now_ts - last_ts < self.cooldown_seconds

    async def _send_private_alert(self, event: AstrMessageEvent, alert_text: str):
        bot = getattr(event, "bot", None)
        if not bot or not self.notify_user_id:
            logger.warning("qq-keyword-alert: 无 bot 或 notify_user_id，无法发送私聊提醒")
            return
        await bot.call_action("send_private_msg", user_id=int(self.notify_user_id), message=alert_text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("添加群")
    async def add_group(self, event: AstrMessageEvent, group_id: str = ""):
        if not await self._require_private_admin(event):
            return
        group_id = group_id.strip()
        if not self._valid_digits(group_id):
            await self._reply(event, "用法：/添加群 <纯数字群号>")
            return
        if group_id in self.rules:
            await self._reply(event, f"群已存在：{group_id}")
            return
        self._ensure_rule(group_id)
        ok, err = self._save()
        await self._reply(event, f"已添加监控群：{group_id}" if ok else f"保存失败：{err}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删除群")
    async def del_group(self, event: AstrMessageEvent, group_id: str = ""):
        if not await self._require_private_admin(event):
            return
        group_id = group_id.strip()
        if not self._valid_digits(group_id):
            await self._reply(event, "用法：/删除群 <纯数字群号>")
            return
        if group_id not in self.rules:
            await self._reply(event, f"群不存在：{group_id}")
            return
        del self.rules[group_id]
        ok, err = self._save()
        await self._reply(event, f"已删除监控群：{group_id}" if ok else f"保存失败：{err}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开启监控")
    async def enable_group(self, event: AstrMessageEvent, group_id: str = ""):
        if not await self._require_private_admin(event):
            return
        group_id = group_id.strip()
        if not self._valid_digits(group_id) or group_id not in self.rules:
            await self._reply(event, "用法：/开启监控 <已存在群号>")
            return
        self.rules[group_id]["enabled"] = True
        ok, err = self._save()
        await self._reply(event, f"已开启监控：{group_id}" if ok else f"保存失败：{err}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关闭监控")
    async def disable_group(self, event: AstrMessageEvent, group_id: str = ""):
        if not await self._require_private_admin(event):
            return
        group_id = group_id.strip()
        if not self._valid_digits(group_id) or group_id not in self.rules:
            await self._reply(event, "用法：/关闭监控 <已存在群号>")
            return
        self.rules[group_id]["enabled"] = False
        ok, err = self._save()
        await self._reply(event, f"已关闭监控：{group_id}" if ok else f"保存失败：{err}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置通知")
    async def set_notify(self, event: AstrMessageEvent, user_id: str = ""):
        if not await self._require_private_admin(event):
            return
        user_id = user_id.strip()
        if not self._valid_digits(user_id):
            await self._reply(event, "用法：/设置通知 <纯数字QQ号>")
            return
        self.notify_user_id = user_id
        ok, err = self._save()
        await self._reply(event, f"已设置通知QQ：{user_id}" if ok else f"保存失败：{err}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("添加")
    async def add_keyword(self, event: AstrMessageEvent, group_id: str = "", payload: str = ""):
        if not await self._require_private_admin(event):
            return
        if not self._valid_digits(group_id):
            await self._reply(event, "用法：/添加 <群号> <关键词1,关键词2>")
            return
        rule = self._ensure_rule(group_id)
        items = [x.strip() for x in re.split(r"[,，\n]", payload) if x.strip()]
        if not items:
            await self._reply(event, "用法：/添加 <群号> <关键词1,关键词2>")
            return
        added = []
        for item in items:
            if item not in rule["keywords"]:
                rule["keywords"].append(item)
                added.append(item)
        if not added:
            await self._reply(event, "没有新增内容。")
            return
        ok, err = self._save()
        await self._reply(event, f"[{group_id}] 已添加关键词：{', '.join(added)}" if ok else f"保存失败：{err}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删除")
    async def del_keyword(self, event: AstrMessageEvent, group_id: str = "", keyword: str = ""):
        if not await self._require_private_admin(event):
            return
        if not self._valid_digits(group_id) or group_id not in self.rules:
            await self._reply(event, "用法：/删除 <群号> <关键词>")
            return
        keyword = keyword.strip()
        if keyword not in self.rules[group_id]["keywords"]:
            await self._reply(event, f"[{group_id}] 不存在：{keyword}")
            return
        self.rules[group_id]["keywords"] = [x for x in self.rules[group_id]["keywords"] if x != keyword]
        ok, err = self._save()
        await self._reply(event, f"[{group_id}] 已删除关键词：{keyword}" if ok else f"保存失败：{err}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("添加过滤")
    async def add_exclude(self, event: AstrMessageEvent, group_id: str = "", payload: str = ""):
        if not await self._require_private_admin(event):
            return
        if not self._valid_digits(group_id):
            await self._reply(event, "用法：/添加过滤 <群号> <过滤词1,过滤词2>")
            return
        rule = self._ensure_rule(group_id)
        items = [x.strip() for x in re.split(r"[,，\n]", payload) if x.strip()]
        if not items:
            await self._reply(event, "用法：/添加过滤 <群号> <过滤词1,过滤词2>")
            return
        added = []
        for item in items:
            if item not in rule["exclude_keywords"]:
                rule["exclude_keywords"].append(item)
                added.append(item)
        if not added:
            await self._reply(event, "没有新增内容。")
            return
        ok, err = self._save()
        await self._reply(event, f"[{group_id}] 已添加过滤词：{', '.join(added)}" if ok else f"保存失败：{err}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删除过滤")
    async def del_exclude(self, event: AstrMessageEvent, group_id: str = "", keyword: str = ""):
        if not await self._require_private_admin(event):
            return
        if not self._valid_digits(group_id) or group_id not in self.rules:
            await self._reply(event, "用法：/删除过滤 <群号> <过滤词>")
            return
        keyword = keyword.strip()
        if keyword not in self.rules[group_id]["exclude_keywords"]:
            await self._reply(event, f"[{group_id}] 过滤词不存在：{keyword}")
            return
        self.rules[group_id]["exclude_keywords"] = [x for x in self.rules[group_id]["exclude_keywords"] if x != keyword]
        ok, err = self._save()
        await self._reply(event, f"[{group_id}] 已删除过滤词：{keyword}" if ok else f"保存失败：{err}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("正则模式")
    async def regex_mode(self, event: AstrMessageEvent, group_id: str = "", mode: str = ""):
        if not await self._require_private_admin(event):
            return
        if not self._valid_digits(group_id) or group_id not in self.rules:
            await self._reply(event, "用法：/正则模式 <群号> 开|关")
            return
        mode = mode.strip()
        if mode not in {"开", "关", "on", "off"}:
            await self._reply(event, "用法：/正则模式 <群号> 开|关")
            return
        self.rules[group_id]["regex_mode"] = mode in {"开", "on"}
        ok, err = self._save()
        status = "开启" if self.rules[group_id]["regex_mode"] else "关闭"
        await self._reply(event, f"[{group_id}] 已{status}正则模式" if ok else f"保存失败：{err}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关键词帮助")
    async def help_cmd(self, event: AstrMessageEvent):
        if not await self._require_private_admin(event):
            return
        text = (
            "[qq-keyword-alert 帮助]\n"
            "/添加群 <群号>\n"
            "/删除群 <群号>\n"
            "/开启监控 <群号>\n"
            "/关闭监控 <群号>\n"
            "/添加 <群号> <关键词1,关键词2>\n"
            "/删除 <群号> <关键词>\n"
            "/添加过滤 <群号> <过滤词1,过滤词2>\n"
            "/删除过滤 <群号> <过滤词>\n"
            "/正则模式 <群号> 开|关\n"
            "/设置通知 <QQ号>\n"
            "/关键词列表\n"
            "/状态\n"
            "/测试通知"
        )
        await self._reply(event, text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("状态")
    async def status(self, event: AstrMessageEvent):
        if not await self._require_private_admin(event):
            return
        enabled = sum(1 for r in self.rules.values() if r.get("enabled"))
        lines = [
            "[qq-keyword-alert 状态]",
            f"监控群数量：{len(self.rules)}",
            f"启用群数量：{enabled}",
            f"通知QQ：{self.notify_user_id or '未设置'}",
            f"冷却：{self.cooldown_seconds}s",
        ]
        await self._reply(event, "\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关键词列表")
    async def list_keywords(self, event: AstrMessageEvent):
        if not await self._require_private_admin(event):
            return
        lines = ["[qq-keyword-alert]"]
        for gid, rule in sorted(self.rules.items()):
            lines.extend([
                f"群 {gid} | {'开启' if rule.get('enabled') else '关闭'} | 正则 {'开' if rule.get('regex_mode') else '关'}",
                f"  关键词：{', '.join(rule.get('keywords', [])) or '空'}",
                f"  过滤词：{', '.join(rule.get('exclude_keywords', [])) or '空'}",
            ])
        lines.append(f"通知QQ：{self.notify_user_id or '未设置'}")
        await self._reply(event, "\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("测试通知")
    async def test_notify(self, event: AstrMessageEvent):
        if not await self._require_private_admin(event):
            return
        try:
            await self._send_private_alert(event, "[qq-keyword-alert] 测试通知成功。")
            await self._reply(event, "测试通知已发送。")
        except Exception as e:
            await self._reply(event, f"测试通知失败：{e}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_group_message(self, event: AstrMessageEvent):
        group_id = str(event.get_group_id() or "").strip()
        rule = self._get_rule(group_id)
        if not group_id or not rule or not rule.get("enabled", True):
            return
        text = (event.message_str or "").strip()
        if not text:
            return
        if self._has_exclude(text, group_id):
            logger.info(f"qq-keyword-alert: filtered -> group={group_id}, text={text[:80]}")
            return
        matched = self._match_keywords(text, group_id)
        if not matched:
            return
        sender_id = str(event.get_sender_id() or "unknown")
        sender_name = getattr(event.message_obj, "sender", None)
        nickname = ""
        if isinstance(sender_name, dict):
            nickname = sender_name.get("nickname") or sender_name.get("card") or ""
        dedupe_key = self._dedupe_key(group_id, sender_id, text)
        now_ts = datetime.now().timestamp()
        if self._in_cooldown(dedupe_key, now_ts):
            logger.info(f"qq-keyword-alert: cooldown -> group={group_id}, sender={sender_id}, hit={matched}")
            return
        self.last_hits[dedupe_key] = now_ts
        try:
            group = await event.get_group()
            group_name = getattr(group, 'group_name', '') or getattr(group, 'name', '') or '未知'
        except Exception:
            group_name = '未知'
        hit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        alert_text = (
            "[关键词命中提醒]\n"
            f"群名: {group_name}\n"
            f"群号: {group_id}\n"
            f"发送者: {nickname or sender_id} ({sender_id})\n"
            f"命中词: {', '.join(matched)}\n"
            f"时间: {hit_time}\n"
            f"内容: {self._highlight_hits(text, matched)}"
        )
        try:
            await self._send_private_alert(event, alert_text)
            logger.info(f"qq-keyword-alert: alerted -> group={group_id}, group_name={group_name}, sender={sender_id}, notify={self.notify_user_id}, hit={matched}")
        except Exception as e:
            logger.error(f"qq-keyword-alert: 发送提醒失败: {e}")
