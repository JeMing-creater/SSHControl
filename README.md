# Control Platform

集中管理多台 Docker 宿主机的端口容器分配平台。

## 功能

- 通过 SSH 管理平台本机和远端 Docker 宿主机
- 按端口创建长期在线 SSH 容器
- 记录使用者、用途、端口、镜像和资源配额
- 默认共享宿主机 `/mnt`
- CPU / 内存在线调整
- 额外挂载变更提示“需要重建”
- 自动环境快照归档到 `/mnt`
- 可视化宿主机资源、容器配额和实时占用
- 新建容器时自动补齐 X11-forwarding 所需的 `xauth` 与 `sshd` 配置

## 运行

```bash
conda activate base
cd /workspace/Control
python run.py
```

默认监听 `0.0.0.0:8080`。

## 说明

- `/workspace` 存储上限当前以数据库记录和界面管理为主，尚未接入底层硬配额实现。
- bind mount 变更会标记为“需重建”，由管理员确认重建后生效。
- 环境快照采用 `docker commit + docker save` 方式归档，不包含用户代码/权重的专门备份逻辑。
- X11-forwarding 当前假设基础镜像为 Ubuntu/Debian 系并可使用 `apt-get`；客户端仍需自备 X server，并通过 `ssh -X` 或 `ssh -Y` 连接。
- 当前平台主路径为 SSH-only；即使管理平台所在服务器也应作为 SSH 宿主机接入，不再使用 local 直控策略。
