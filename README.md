# Control Platform

Docker 多宿主机分配与监控平台。

## 功能

- SSH 接入多台 Docker 宿主机
- 按端口分配容器
- 管理用户、管理员和使用者登录
- 资源监控：CPU / 内存 / GPU 显存 / Docker 磁盘
- 宿主机与容器的实时状态缓存
- 快照、终端和批量维护

## 运行环境

- Python 3.11+
- Conda base 环境可直接运行
- 已安装 Docker 和 SSH 可达的宿主机

## 快速开始

```bash
cp .env.example .env
conda activate base
pip install -r requirements.txt
python run.py
```

默认会从 `8080` 起寻找可用端口并启动服务。

## 配置项

主要环境变量均以 `CONTROL_` 为前缀，见 [.env.example](./.env.example)。

常用项：

- `CONTROL_DATABASE_URL`：SQLite 或其他 SQLAlchemy 数据库连接串
- `CONTROL_ROOT_ADMIN_ACCOUNT` / `CONTROL_ROOT_ADMIN_PASSWORD`：根管理员账号
- `CONTROL_AUTH_SECRET_KEY`：登录会话签名密钥
- `CONTROL_DEFAULT_WORKSPACE_ROOT`：宿主机工作目录
- `CONTROL_DEFAULT_SNAPSHOT_ROOT`：快照目录
- `CONTROL_BIND_PORT`：启动监听端口

## 数据库

默认使用 `data/platform.db`。

- 首次启动会自动建表
- `data/` 下的数据库文件不建议直接提交到 GitHub
- 如需迁移到其他服务器，保留代码、`.env` 和宿主机接入配置即可

## 部署注意

- 宿主机必须能通过 SSH 登录，并可访问 Docker
- `workspace_root` 需指向真实存在的目录
- 若使用 SMTP 注册通知，请配置 `CONTROL_SMTP_*`
- 平台支持长期运行，宿主机状态由后台定时刷新

## 目录

- `app/`：后端代码
- `templates/`：Jinja2 页面
- `static/`：样式和静态资源
- `data/`：本地数据库

## 许可证

未指定。
