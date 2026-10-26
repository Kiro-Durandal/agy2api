from typing import Any, Dict

from src.converter.thoughtSignature_fix import (
    is_internal_placeholder_text,
    is_skip_thought_signature_placeholder,
)


def extract_content_and_reasoning(parts: list) -> tuple:
    """从Gemini响应部件中提取内容和推理内容

    Args:
        parts: Gemini 响应中的 parts 列表

    Returns:
        (content, reasoning_content, images): 文本内容、推理内容和图片数据的元组
        - content: 文本内容字符串
        - reasoning_content: 推理内容字符串
        - images: 图片数据列表,每个元素格式为:
          {
              "type": "image_url",
              "image_url": {
                  "url": "data:{mime_type};base64,{base64_data}"
              }
          }
    """
    content = ""
    reasoning_content = ""
    images = []

    for part in parts:
        if is_skip_thought_signature_placeholder(part):
            continue

        # 提取文本内容
        text = part.get("text", "")
        if is_internal_placeholder_text(text):
            continue
        if text:
            if part.get("thought", False):
                reasoning_content += text
            else:
                content += text

        # 提取图片数据
        if "inlineData" in part:
            inline_data = part["inlineData"]
            mime_type = inline_data.get("mimeType", "image/png")
            base64_data = inline_data.get("data", "")
            images.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{base64_data}"
                }
            })

    return content, reasoning_content, images


async def merge_system_messages(request_body: Dict[str, Any]) -> Dict[str, Any]:
    """
    将 system/developer 指令收集到 Gemini 的 systemInstruction。

    Gemini contents 不支持这两种角色。即使指令出现在历史对话中间，
    也不能将其降级为 user 消息；按原有顺序收集指令，并从 contents 中移除。
    顶层 system 参数用于 Anthropic 格式，排在消息中的指令之前。

    Args:
        request_body: OpenAI 或 Claude 格式的请求体，包含 messages 字段

    Returns:
        处理后的请求体
    """
    # 处理 Anthropic 格式的顶层 system 参数
    # Anthropic API 规范: system 是顶层参数，不在 messages 中
    system_content = request_body.get("system")
    if system_content:
        system_parts = []

        if isinstance(system_content, str):
            if system_content.strip():
                system_parts.append({"text": system_content})
        elif isinstance(system_content, list):
            # system 可以是包含多个块的列表
            for item in system_content:
                if isinstance(item, dict):
                    if item.get("type") == "text" and item.get("text", "").strip():
                        system_parts.append({"text": item["text"]})
                elif isinstance(item, str) and item.strip():
                    system_parts.append({"text": item})

        if system_parts:
            request_body = request_body.copy()
            request_body.pop("system", None)
            request_body["systemInstruction"] = {"parts": system_parts}

    messages = request_body.get("messages", [])
    if not messages:
        return request_body

    # 提取所有 system/developer 消息。Gemini 只有一个顶层指令字段，
    # 因而无法保留这些指令在对话历史中的原始位置。
    system_parts = []

    # 如果已经从顶层 system 参数创建了 systemInstruction，获取现有的 parts
    if "systemInstruction" in request_body:
        existing_instruction = request_body.get("systemInstruction", {})
        if isinstance(existing_instruction, dict):
            system_parts = existing_instruction.get("parts", []).copy()

    remaining_messages = []
    instruction_roles = ("system", "developer")

    for message in messages:
        role = message.get("role", "")
        content = message.get("content", "")

        if role in instruction_roles:
            # 提取指令消息的文本内容
            if isinstance(content, str):
                if content.strip():
                    system_parts.append({"text": content})
            elif isinstance(content, list):
                # 处理列表格式的 content
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text" and item.get("text", "").strip():
                            system_parts.append({"text": item["text"]})
                    elif isinstance(item, str) and item.strip():
                        system_parts.append({"text": item})
        else:
            remaining_messages.append(message)

    # 如果没有指令内容，仍须移除空的指令消息，避免传给上游。
    if not system_parts:
        if remaining_messages == messages:
            return request_body
        result = request_body.copy()
        result["messages"] = remaining_messages
        return result

    # 构建新的请求体
    result = request_body.copy()

    # 添加或更新 systemInstruction
    result["systemInstruction"] = {"parts": system_parts}

    # 更新 messages 列表（移除已处理的 system 消息）
    result["messages"] = remaining_messages

    return result
