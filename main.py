"""群聊管理插件 - Group Manager Plugin

提供群聊管理功能，包括：
- 核心功能：禁言/解除禁言、设置名片、撤回消息、查询成员
- 可选功能：踢人、全员禁言（需手动开启）

安全机制：
- 配置 allow_ai_autonomous 为 true 时，所有工具调用直接放行（AI 自主执行）
- 为 false 时，仅管理员列表中的用户和机器人自身可执行
"""

from core.plugin import BasePlugin, logger, on, Priority, register
from core.chat.message_utils import KiraMessageBatchEvent, KiraMessageEvent
from core.provider import LLMRequest
from core.prompt_manager import Prompt
from typing import Optional


# ============ 常量定义 ============

TOOLS_PROMPT_TEMPLATE = """\
## 群聊管理工具使用说明

你是群聊助手，当管理员需要管理群聊时，可以使用以下工具：

{tools_list}

### 使用规则
1. 【重要】只有管理员（在admin_qq_list中的用户）可以使用这些功能
2. 使用工具前，先确认用户身份，非管理员请求应礼貌拒绝
3. 执行成功后简要报告操作结果
4. 如Bot不是群管理员，操作会失败，请提示用户设置Bot为管理员

### 示例场景
- 用户："把发广告的禁言10分钟" → 使用 group_ban_user
- 用户："查看群成员列表" → 使用 group_get_member_list
- 用户："修改我的群名片为xxx" → 使用 group_set_card
"""

CORE_TOOLS_DESC = """
- group_ban_user: 禁言指定群成员。参数：user_id(QQ号), duration(秒，默认600)
- group_unban_user: 解除指定群成员的禁言。参数：user_id(QQ号)
- group_set_card: 设置群成员的群名片。参数：user_id(QQ号), card(新名片)
- group_delete_msg: 撤回指定消息。参数：message_id(消息ID)
- group_get_member_list: 获取群成员列表（简要信息）
- group_get_member_info: 获取指定成员详细信息。参数：user_id(QQ号)
"""

OPTIONAL_TOOLS_DESC = {
    "kick": "- group_kick_user: 【高危】踢出群成员。参数：user_id(QQ号), reject_add_request(是否拒绝加群申请，默认false)",
    "whole_ban": "- group_whole_ban: 【高危】全员禁言/解除全员禁言。参数：enable(true/false)"
}


class GroupManagerPlugin(BasePlugin):
    """
    群聊管理插件主类
    """
    
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        # 管理员列表
        raw_admin_list = cfg.get("admin_qq_list", [])
        self.admin_list = [str(uid) for uid in raw_admin_list if uid]
        self.enable_kick = cfg.get("enable_kick_user", False)
        self.enable_whole_ban = cfg.get("enable_whole_ban", False)
        self.auto_check_admin = cfg.get("auto_check_admin", True)
        self.log_operations = cfg.get("log_operations", True)
        # 新增：允许 AI 自主执行（跳过权限检查）
        self.allow_ai_autonomous = cfg.get("allow_ai_autonomous", True)
        
    async def initialize(self):
        logger.info(f"[GroupManager] 群聊管理插件已加载")
        logger.info(f"[GroupManager] 管理员列表: {self.admin_list}")
        logger.info(f"[GroupManager] 踢人功能: {'已启用' if self.enable_kick else '已禁用'}")
        logger.info(f"[GroupManager] 全员禁言功能: {'已启用' if self.enable_whole_ban else '已禁用'}")
        logger.info(f"[GroupManager] AI自主执行模式: {'开启' if self.allow_ai_autonomous else '关闭'}")
    
    async def terminate(self):
        logger.info("[GroupManager] 群聊管理插件已卸载")
    
    # ============ 权限验证 ============
    
    def _is_admin(self, event: KiraMessageBatchEvent) -> bool:
        """
        检查操作者是否为管理员
        如果 allow_ai_autonomous 为 True，直接放行所有调用
        否则按照原有逻辑检查
        """
        if self.allow_ai_autonomous:
            logger.debug("[GroupManager] AI自主执行模式已开启，直接放行")
            return True
        
        # 以下为原有权限检查逻辑（当自主模式关闭时生效）
        if not event.messages:
            logger.warning("[GroupManager] event.messages 为空，拒绝")
            return False
        
        last_message = event.messages[-1]
        sender_qq = str(last_message.sender.user_id) if last_message.sender else None
        self_qq = str(last_message.self_id) if hasattr(last_message, 'self_id') else None
        
        logger.debug(f"[GroupManager] 权限检查: 发送者={sender_qq}, Bot={self_qq}, 管理员列表={self.admin_list}")
        
        # 发送者是机器人自身
        if self_qq and sender_qq == self_qq:
            logger.debug("[GroupManager] 机器人自身，允许")
            return True
        
        # 系统消息（提醒插件等）
        if sender_qq in ("system", "unknown", "", "None"):
            logger.debug("[GroupManager] 系统消息，允许")
            return True
        
        # 发送者在管理员列表中
        if sender_qq in self.admin_list:
            logger.debug("[GroupManager] 发送者在管理员列表中，允许")
            return True
        
        logger.debug("[GroupManager] 权限不足，拒绝")
        return False
    
    def _log_operation(self, operation: str, operator: str, target: str = "", result: str = ""):
        if not self.log_operations:
            return
        target_str = f", 目标: {target}" if target else ""
        logger.info(f"[GroupManager] {operation} | 操作者: {operator}{target_str} | 结果: {result}")
    
    def _get_qq_client(self, event: KiraMessageBatchEvent):
        try:
            adapter_name = event.adapter.name if event.adapter else "qq"
            adapter = self.ctx.adapter_mgr.get_adapter(adapter_name)
            if not adapter:
                logger.error(f"[GroupManager] 适配器 '{adapter_name}' 不存在")
                return None
            return adapter.get_client()
        except Exception as e:
            logger.error(f"[GroupManager] 获取适配器失败: {e}")
            return None
    
    # ============ LLM提示注入 ============
    
    @on.llm_request(priority=Priority.MEDIUM)
    async def inject_tools_prompt(self, event: KiraMessageBatchEvent, req: LLMRequest, *_):
        if not event.is_group_message():
            return
        
        tools_list = CORE_TOOLS_DESC
        if self.enable_kick:
            tools_list += "\n" + OPTIONAL_TOOLS_DESC["kick"]
        if self.enable_whole_ban:
            tools_list += "\n" + OPTIONAL_TOOLS_DESC["whole_ban"]
        
        prompt_content = TOOLS_PROMPT_TEMPLATE.format(tools_list=tools_list)
        req.system_prompt.append(Prompt(
            name="group_manager_tools",
            content=prompt_content
        ))
    
    # ============ 核心工具：禁言 ============
    
    @register.tool(
        name="group_ban_user",
        description="【需要管理员权限】禁言指定群成员",
        params={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "要禁言的QQ号"},
                "duration": {"type": "integer", "description": "禁言时长（秒），默认600秒（10分钟）", "default": 600}
            },
            "required": ["user_id"]
        }
    )
    async def ban_user(self, event: KiraMessageBatchEvent, user_id: str, duration: int = 600) -> str:
        if not self._is_admin(event):
            return "❌ 用户不是插件的管理员"
        
        operator = str(event.messages[-1].sender.user_id) if event.messages else "系统"
        group_id = event.session.session_id
        client = self._get_qq_client(event)
        if not client:
            return "❌ 无法连接到QQ客户端"
        
        try:
            result = await client.send_action("set_group_ban", {
                "group_id": group_id,
                "user_id": user_id,
                "duration": duration
            })
            if result.get("status") == "ok":
                duration_min = duration // 60
                self._log_operation("禁言", operator, user_id, f"成功，时长{duration_min}分钟")
                return f"✅ 已禁言用户 {user_id}，时长 {duration_min} 分钟"
            else:
                err_msg = result.get("message", "未知错误")
                self._log_operation("禁言", operator, user_id, f"失败: {err_msg}")
                return f"❌ 禁言失败: {err_msg}"
        except Exception as e:
            logger.error(f"[GroupManager] 禁言操作异常: {e}")
            return f"❌ 禁言操作异常: {str(e)}"
    
    @register.tool(
        name="group_unban_user",
        description="【需要管理员权限】解除指定群成员的禁言",
        params={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "要解除禁言的QQ号"}
            },
            "required": ["user_id"]
        }
    )
    async def unban_user(self, event: KiraMessageBatchEvent, user_id: str) -> str:
        if not self._is_admin(event):
            return "❌ 用户不是插件的管理员"
        
        operator = str(event.messages[-1].sender.user_id) if event.messages else "系统"
        group_id = event.session.session_id
        client = self._get_qq_client(event)
        if not client:
            return "❌ 无法连接到QQ客户端"
        
        try:
            result = await client.send_action("set_group_ban", {
                "group_id": group_id,
                "user_id": user_id,
                "duration": 0
            })
            if result.get("status") == "ok":
                self._log_operation("解除禁言", operator, user_id, "成功")
                return f"✅ 已解除用户 {user_id} 的禁言"
            else:
                err_msg = result.get("message", "未知错误")
                self._log_operation("解除禁言", operator, user_id, f"失败: {err_msg}")
                return f"❌ 解除禁言失败: {err_msg}"
        except Exception as e:
            logger.error(f"[GroupManager] 解除禁言异常: {e}")
            return f"❌ 解除禁言异常: {str(e)}"
    
    # ============ 核心工具：群名片 ============
    
    @register.tool(
        name="group_set_card",
        description="【需要管理员权限】设置群成员的群名片",
        params={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "目标QQ号"},
                "card": {"type": "string", "description": "新的群名片（空字符串表示取消名片）"}
            },
            "required": ["user_id", "card"]
        }
    )
    async def set_card(self, event: KiraMessageBatchEvent, user_id: str, card: str) -> str:
        if not self._is_admin(event):
            return "❌ 用户不是插件的管理员"
        
        operator = str(event.messages[-1].sender.user_id) if event.messages else "系统"
        group_id = event.session.session_id
        client = self._get_qq_client(event)
        if not client:
            return "❌ 无法连接到QQ客户端"
        
        try:
            result = await client.send_action("set_group_card", {
                "group_id": group_id,
                "user_id": user_id,
                "card": card
            })
            if result.get("status") == "ok":
                card_display = card if card else "(取消名片)"
                self._log_operation("设置名片", operator, user_id, f"成功: {card_display}")
                return f"✅ 已设置用户 {user_id} 的群名片为: {card_display}"
            else:
                err_msg = result.get("message", "未知错误")
                self._log_operation("设置名片", operator, user_id, f"失败: {err_msg}")
                return f"❌ 设置名片失败: {err_msg}"
        except Exception as e:
            logger.error(f"[GroupManager] 设置名片异常: {e}")
            return f"❌ 设置名片异常: {str(e)}"
    
    # ============ 核心工具：撤回消息 ============
    
    @register.tool(
        name="group_delete_msg",
        description="【需要管理员权限】撤回指定消息",
        params={
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "要撤回的消息ID"}
            },
            "required": ["message_id"]
        }
    )
    async def delete_msg(self, event: KiraMessageBatchEvent, message_id: str) -> str:
        if not self._is_admin(event):
            return "❌ 用户不是插件的管理员"
        
        operator = str(event.messages[-1].sender.user_id) if event.messages else "系统"
        client = self._get_qq_client(event)
        if not client:
            return "❌ 无法连接到QQ客户端"
        
        try:
            result = await client.send_action("delete_msg", {"message_id": message_id})
            if result.get("status") == "ok":
                self._log_operation("撤回消息", operator, "", f"成功, 消息ID: {message_id}")
                return f"✅ 已撤回消息 (ID: {message_id})"
            else:
                err_msg = result.get("message", "未知错误")
                self._log_operation("撤回消息", operator, "", f"失败: {err_msg}")
                return f"❌ 撤回消息失败: {err_msg}"
        except Exception as e:
            logger.error(f"[GroupManager] 撤回消息异常: {e}")
            return f"❌ 撤回消息异常: {str(e)}"
    
    # ============ 核心工具：查询成员 ============
    
    @register.tool(
        name="group_get_member_list",
        description="【需要管理员权限】获取群成员列表（简要信息）",
        params={"type": "object", "properties": {}}
    )
    async def get_member_list(self, event: KiraMessageBatchEvent) -> str:
        if not self._is_admin(event):
            return "❌ 用户不是插件的管理员"
        
        operator = str(event.messages[-1].sender.user_id) if event.messages else "系统"
        group_id = event.session.session_id
        client = self._get_qq_client(event)
        if not client:
            return "❌ 无法连接到QQ客户端"
        
        try:
            result = await client.send_action("get_group_member_list", {"group_id": group_id})
            if result.get("status") == "ok":
                members = result.get("data", [])
                total = len(members)
                member_preview = []
                for m in members[:10]:
                    user_id = m.get("user_id", "")
                    nickname = m.get("nickname", "")
                    card = m.get("card", "")
                    display = f"{card}({user_id})" if card else f"{nickname}({user_id})"
                    member_preview.append(display)
                preview_str = "\n".join(member_preview)
                more_str = f"\n... 等共 {total} 人" if total > 10 else ""
                self._log_operation("获取成员列表", operator, "", f"成功, 共{total}人")
                return f"📋 群成员列表（共{total}人）：\n{preview_str}{more_str}"
            else:
                err_msg = result.get("message", "未知错误")
                self._log_operation("获取成员列表", operator, "", f"失败: {err_msg}")
                return f"❌ 获取成员列表失败: {err_msg}"
        except Exception as e:
            logger.error(f"[GroupManager] 获取成员列表异常: {e}")
            return f"❌ 获取成员列表异常: {str(e)}"
    
    @register.tool(
        name="group_get_member_info",
        description="【需要管理员权限】获取指定群成员的详细信息",
        params={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "要查询的QQ号"}
            },
            "required": ["user_id"]
        }
    )
    async def get_member_info(self, event: KiraMessageBatchEvent, user_id: str) -> str:
        if not self._is_admin(event):
            return "❌ 用户不是插件的管理员"
        
        operator = str(event.messages[-1].sender.user_id) if event.messages else "系统"
        group_id = event.session.session_id
        client = self._get_qq_client(event)
        if not client:
            return "❌ 无法连接到QQ客户端"
        
        try:
            result = await client.send_action("get_group_member_info", {
                "group_id": group_id,
                "user_id": user_id
            })
            if result.get("status") == "ok":
                data = result.get("data", {})
                info_lines = [
                    f"📋 成员信息：",
                    f"QQ号: {data.get('user_id', 'N/A')}",
                    f"昵称: {data.get('nickname', 'N/A')}",
                    f"群名片: {data.get('card', '未设置')}",
                    f"群等级: {data.get('level', 'N/A')}",
                    f"头衔: {data.get('title', '无')}",
                    f"入群时间: {self._format_time(data.get('join_time', 0))}",
                    f"最后发言: {self._format_time(data.get('last_sent_time', 0))}",
                ]
                role = data.get('role', 'member')
                role_map = {'owner': '群主', 'admin': '管理员', 'member': '普通成员'}
                info_lines.append(f"身份: {role_map.get(role, role)}")
                shut_up_timestamp = data.get('shut_up_timestamp', 0)
                if shut_up_timestamp > 0:
                    info_lines.append("⛔ 当前处于禁言状态")
                info_str = "\n".join(info_lines)
                self._log_operation("获取成员信息", operator, user_id, "成功")
                return info_str
            else:
                err_msg = result.get("message", "未知错误")
                self._log_operation("获取成员信息", operator, user_id, f"失败: {err_msg}")
                return f"❌ 获取成员信息失败: {err_msg}"
        except Exception as e:
            logger.error(f"[GroupManager] 获取成员信息异常: {e}")
            return f"❌ 获取成员信息异常: {str(e)}"
    
    # ============ 可选工具：踢人 ============
    
    @register.tool(
        name="group_kick_user",
        description="【需要管理员权限】【高危操作】踢出指定群成员（需在配置中启用）",
        params={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "要踢出的QQ号"},
                "reject_add_request": {"type": "boolean", "description": "是否拒绝该用户的加群申请", "default": False}
            },
            "required": ["user_id"]
        }
    )
    async def kick_user(self, event: KiraMessageBatchEvent, user_id: str, reject_add_request: bool = False) -> str:
        if not self.enable_kick:
            return "❌ 踢人功能未启用，请在插件配置中开启"
        if not self._is_admin(event):
            return "❌ 用户不是插件的管理员"
        
        operator = str(event.messages[-1].sender.user_id) if event.messages else "系统"
        group_id = event.session.session_id
        client = self._get_qq_client(event)
        if not client:
            return "❌ 无法连接到QQ客户端"
        
        try:
            result = await client.send_action("set_group_kick", {
                "group_id": group_id,
                "user_id": user_id,
                "reject_add_request": reject_add_request
            })
            if result.get("status") == "ok":
                reject_str = "，已拒绝加群申请" if reject_add_request else ""
                self._log_operation("踢出成员【高危】", operator, user_id, f"成功{reject_str}")
                return f"✅ 已将用户 {user_id} 踢出群聊{reject_str}"
            else:
                err_msg = result.get("message", "未知错误")
                self._log_operation("踢出成员", operator, user_id, f"失败: {err_msg}")
                return f"❌ 踢出成员失败: {err_msg}"
        except Exception as e:
            logger.error(f"[GroupManager] 踢出成员异常: {e}")
            return f"❌ 踢出成员异常: {str(e)}"
    
    # ============ 可选工具：全员禁言 ============
    
    @register.tool(
        name="group_whole_ban",
        description="【需要管理员权限】【高危操作】开启/关闭全员禁言（需在配置中启用）",
        params={
            "type": "object",
            "properties": {
                "enable": {"type": "boolean", "description": "true开启全员禁言，false关闭"}
            },
            "required": ["enable"]
        }
    )
    async def whole_ban(self, event: KiraMessageBatchEvent, enable: bool) -> str:
        if not self.enable_whole_ban:
            return "❌ 全员禁言功能未启用，请在插件配置中开启"
        if not self._is_admin(event):
            return "❌ 用户不是插件的管理员"
        
        operator = str(event.messages[-1].sender.user_id) if event.messages else "系统"
        group_id = event.session.session_id
        client = self._get_qq_client(event)
        if not client:
            return "❌ 无法连接到QQ客户端"
        
        try:
            result = await client.send_action("set_group_whole_ban", {
                "group_id": group_id,
                "enable": enable
            })
            if result.get("status") == "ok":
                action_str = "开启" if enable else "关闭"
                self._log_operation(f"{action_str}全员禁言【高危】", operator, "", "成功")
                return f"✅ 已{action_str}全员禁言"
            else:
                err_msg = result.get("message", "未知错误")
                self._log_operation(f"全员禁言操作", operator, "", f"失败: {err_msg}")
                return f"❌ 全员禁言操作失败: {err_msg}"
        except Exception as e:
            logger.error(f"[GroupManager] 全员禁言异常: {e}")
            return f"❌ 全员禁言异常: {str(e)}"
    
    # ============ 辅助方法 ============
    
    @staticmethod
    def _format_time(timestamp: int) -> str:
        if not timestamp:
            return "N/A"
        try:
            from datetime import datetime
            dt = datetime.fromtimestamp(timestamp)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except:
            return str(timestamp)
