# hermes-yzj

云之家（YunZhiJia）平台适配器插件，为 [Hermes](https://github.com/NousResearch/hermes-agent) 提供云之家机器人的接入能力。

## 安装

### 一键安装

下载并执行安装脚本：

```bash
curl -fsSL https://raw.githubusercontent.com/JanonAI/hermes-yzj/main/install.sh | bash
```

或手动下载后执行：

```bash
curl -O https://raw.githubusercontent.com/JanonAI/hermes-yzj/main/install.sh
bash install.sh
```

脚本会自动从 GitHub 下载最新版本、解压并安装到 Hermes 插件目录。

如果 Hermes 数据目录不在默认位置（`~/.hermes`），可通过参数指定：

```bash
bash install.sh /path/to/hermes-data
```

或通过环境变量：

```bash
HERMES_HOME=/path/to/hermes-data bash install.sh
```

### 安装后配置

安装完成后，运行配置向导：

```bash
hermes gateway setup
```

在菜单中选择 **🤖 YZJ Robot**，按引导输入凭据（支持 App API 模式和旧版 Webhook 模式二选一）。配置完成后重启 gateway 生效：

```bash
hermes gateway restart
```

---

## 插件介绍

### 功能概述

- **入站**：通过 WebSocket 长连接实时接收云之家机器人消息
- **出站**：发送文本消息和图片消息
- **多账户**：支持在同一 Hermes 实例中同时接入多个云之家机器人
- **自动重连**：WebSocket 断线后指数退避自动重连
- **消息去重**：基于 `msgId` 在 60 秒窗口内去重，避免重复处理

### 凭据模式

插件支持两种凭据模式，可混合使用：

| 模式 | 入站 | 出站 | 适用场景 |
|------|------|------|----------|
| `app_id` + `app_secret` | WebSocket + accessToken | App API | 新版应用机器人 |
| `send_msg_url`（含 yzjtoken）| WebSocket + yzjtoken | Webhook | 旧版群机器人 |

### 配置方式

#### 方式一：hermes gateway setup（推荐）

运行 `hermes gateway setup`，选择 🤖 YZJ Robot，按向导提示输入凭据。凭据会保存到 `~/.hermes/.env`。

#### 方式二：环境变量

在 `~/.hermes/.env` 中直接设置：

**App API 模式：**

```env
YZJ_APP_ID=your_app_id
YZJ_APP_SECRET=your_app_secret
# 可选
YZJ_ENDPOINT=https://yunzhijia.com
YZJ_HOME_CHANNEL=default@group:your_group_id
```

**旧版 Webhook 模式：**

```env
YZJ_SEND_MSG_URL=https://www.yunzhijia.com/gateway/robot/send?yzjtoken=xxx
```

#### 方式三：config.yaml（多账户场景）

单账户 · App API 模式：

```yaml
yzj:
  app_id: your_app_id
  app_secret: your_app_secret
  endpoint: https://yunzhijia.com  # 可选，默认值
  timeout: 10                       # 可选，默认 10 秒
```

单账户 · 旧版机器人模式：

```yaml
yzj:
  send_msg_url: https://www.yunzhijia.com/gateway/robot/send?yzjtoken=xxx
```

多账户混合模式：

```yaml
yzj:
  endpoint: https://yunzhijia.com
  accounts:
    new_bot:
      app_id: app_id_1
      app_secret: secret_1
    legacy_bot:
      send_msg_url: https://www.yunzhijia.com/gateway/robot/send?yzjtoken=xxx
```

多账户时，`chat_id` 格式为 `{account}@group:{groupId}` 或 `{account}@user:{openId}`。

### 消息类型

- **群消息**：`chat_type = "group"`，`chat_id` 为 `{account}@group:{groupId}`
- **私聊消息**（单聊或私有机器人群）：`chat_type = "dm"`，`chat_id` 为 `{account}@user:{openId}`
- 以 `/` 开头的消息自动识别为 `MessageType.COMMAND`

### 图片发送

App API 模式下，插件会先将图片上传到云之家文件服务，再以 `msgType=23` 发送。如果上传失败，自动降级为发送图片 URL 文本。

