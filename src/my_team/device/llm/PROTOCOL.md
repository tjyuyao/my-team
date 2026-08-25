# LLM 设备协议

LLM 设备是设备进程，它经 application 事件承接 llm_request 并产出 llm_result。
本文件定义设备业务协议；SPEC 不承载设备细节。

## 请求事件 llm_request

请求方发出，target 为设备 pid，source 由宿主注入。payload 字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| command | str | 固定 `"llm_request"` |
| model | str | 模型名 |
| system | str | 系统提示，可选 |
| messages | list | wire 格式消息，见下 |
| tools | list | 工具 schema 列表，可选 |

实例：

```json
{
  "source": "9f8e7d6c5b4a",
  "target": "1a2b3c4d5e6f",
  "kind": "application",
  "payload": {
    "command": "llm_request",
    "model": "deepseek-v4-flash",
    "system": "你是一个简洁的助手。",
    "messages": [
      {"role": "user", "content": "用一句话介绍你自己。"}
    ]
  }
}
```

## 响应事件 llm_result

设备产出，target 回填请求方 pid。payload 字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| command | str | 固定 `"llm_result"` |
| ok | bool | 是否成功 |
| content | str | ok 时可见文本 |
| tool_calls | list | 工具调用列表，无则空列表 |
| usage | dict | provider 用量，失败时为 null |
| error | str | 失败原因，成功时为 null |

成功实例：

```json
{
  "source": "1a2b3c4d5e6f",
  "target": "9f8e7d6c5b4a",
  "kind": "application",
  "payload": {
    "command": "llm_result",
    "ok": true,
    "content": "我是一个简洁的AI助手，乐于提供准确高效的帮助。",
    "tool_calls": [],
    "usage": {
      "input": 94,
      "output": 57,
      "cacheRead": 0,
      "cacheWrite": 0,
      "reasoning": 44,
      "totalTokens": 151,
      "cost": {"input": 0.0, "output": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0, "total": 0.0}
    },
    "error": null
  }
}
```

失败实例：

```json
{
  "source": "1a2b3c4d5e6f",
  "target": "9f8e7d6c5b4a",
  "kind": "application",
  "payload": {
    "command": "llm_result",
    "ok": false,
    "content": "",
    "tool_calls": null,
    "usage": null,
    "error": "provider 错误信息"
  }
}
```

## messages wire 格式

| role | 字段 | 说明 |
| --- | --- | --- |
| user | content: str | 用户消息 |
| assistant | content: str, tool_calls?: [{id, name, arguments}] | 助手消息，可携带工具调用 |
| tool | tool_call_id: str, content: str, is_error?: bool | 工具结果 |

实例：

```json
{"role": "user", "content": "今天天气如何？"}
{"role": "assistant", "content": "我来查询一下。",
 "tool_calls": [{"id": "call_1", "name": "get_weather", "arguments": {"city": "北京"}}]}
{"role": "tool", "tool_call_id": "call_1", "content": "晴，25 度", "is_error": false}
```

## tools wire 格式

```json
{"name": "get_weather", "description": "查询城市天气",
 "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}
```

## 当前实现行为（临时方案，未定稿）

- provider 由工厂在设备进程内创建（HTTP 客户端不可跨进程 pickle）。
- 设备串行处理请求，其余在 inbox 排队（并发 = 1）。
- 凭据经环境变量 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` 注入。
