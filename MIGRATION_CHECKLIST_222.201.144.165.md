# Migration Checklist

目标服务器：
- `root@222.201.144.165 -p 50000`
- 部署目录：`/workspace/Control/`

## 快速步骤

1. 复制当前项目目录到目标机
   - 源：`/workspace/control/`
   - 目标：`/workspace/Control/`

2. 使用目标机现有 conda
   - 直接使用 `/opt/conda`
   - 环境：`base`

3. 安装依赖
   - `pip install -r requirements.txt`
   - 必要时对照 `HANDOFF_222.201.144.165.md` 中的 `pip freeze` 补齐

4. 启动平台
   - `conda activate base`
   - `cd /workspace/Control`
   - `python run.py`

5. 如果 `8080` 被占用
   - `fuser -k 8080/tcp`
   - `python run.py`

6. 首次启动后验证
   - `curl http://127.0.0.1:8080/`
   - 页面能打开

7. 配置远端宿主机时必须注意
   - 非本机地址不要用 `auth_type=local`
   - 应使用 `password` 或 `key`

8. 验证远端 Docker 读取能力
   - `docker info`
   - `docker ps`
   - `docker stats --no-stream`

9. 验证容器创建与失败清场
   - 成功创建一次
   - 故意制造一次失败，确认不会残留脏容器/脏端口/脏数据库状态

10. 阅读完整交接说明
   - `/workspace/Control/HANDOFF_222.201.144.165.md`
