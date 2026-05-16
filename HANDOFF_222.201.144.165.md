# Control Platform Handoff

目标服务器：
- SSH: `root@222.201.144.165 -p 50000`
- 密码: `123456`
- 目标部署目录: `/workspace/Control/`

当前源目录：
- `/workspace/control/`

## 1. 项目目标

这是一个基于 `Python + FastAPI + Docker + SSH` 的多宿主机 Docker 管理平台。

核心目标：
- 通过一个 Web 平台管理多台宿主机
- 通过 SSH 远程控制宿主机 Docker，而不是依赖平台所在本机直接控制
- 按端口分配 SSH 可登录的 Docker 容器
- 记录使用者、用途、端口、镜像、密码、资源配额
- 支持运行后动态调整 CPU / 内存
- 默认共享 `/mnt`
- 支持环境快照
- 支持远端宿主机 Docker 现状可视化

当前用户的明确偏好：
- 更重视“通过 SSH 控制远端宿主机”这一模式
- 不希望“本机直控”成为平台的核心路径
- 平台需要能看到宿主机真实 Docker 状态，而不只是数据库记录
- 平台失败后要尽量自动清理脏容器、脏端口、脏数据库状态

## 2. 当前代码结构

- 入口: `/workspace/control/run.py`
- 主应用: `/workspace/control/app/main.py`
- 路由: `/workspace/control/app/routes/web.py`
- 配置: `/workspace/control/app/config.py`
- 数据库初始化: `/workspace/control/app/db.py`
- 数据模型: `/workspace/control/app/models.py`
- SSH 执行器: `/workspace/control/app/services/ssh_client.py`
- Docker 控制层: `/workspace/control/app/services/docker_engine.py`
- 准入逻辑: `/workspace/control/app/services/admission.py`
- 快照逻辑: `/workspace/control/app/services/snapshots.py`
- 宿主机汇总: `/workspace/control/app/services/metrics.py`
- 前端模板:
  - `/workspace/control/templates/dashboard.html`
  - `/workspace/control/templates/hosts.html`
  - `/workspace/control/templates/host_detail.html`
- 样式: `/workspace/control/static/css/app.css`
- SQLite 数据库: `/workspace/control/data/platform.db`

## 3. 运行环境

目标服务器建议直接使用：
- `/opt/conda` 的 `base` 环境

已知 `pip freeze` 关键依赖版本：

```txt
docker==7.1.0
fastapi==0.136.1
Jinja2==3.1.6
paramiko==5.0.0
pydantic==2.13.4
pydantic-settings==2.14.1
python-multipart==0.0.28
requests==2.34.1
SQLAlchemy==2.0.49
uvicorn==0.46.0
pypinyin==0.55.0
```

最小 requirements 在：
- `/workspace/control/requirements.txt`

但真实环境比 requirements 更完整，建议在新机器上：
1. 直接使用 `/opt/conda` 的 `base` 环境
2. 先安装 `requirements.txt`
3. 再根据 `pip freeze` 补齐缺失包

## 4. 数据库模型

主要表：
- `managed_hosts`
- `allocations`
- `snapshot_records`

重要约束：
- `managed_hosts.name` 唯一
- `allocations.container_name` 唯一

这意味着同一个端口对应的容器名通常是固定的：
- `slugify(host.name) + "-" + host_port`

所以后续创建时不能简单重复插入新 allocation，必须复用历史 `deleted` 记录，否则会触发：
- `sqlite3.IntegrityError: UNIQUE constraint failed: allocations.container_name`

当前这个坑已经修过。

## 5. 当前设计与功能状态

### 5.1 宿主机管理

平台支持添加宿主机：
- 地址
- SSH 端口
- SSH 用户
- 认证方式
- 工作目录根
- `/mnt` 共享路径
- 快照根目录
- 资源预留策略

重要修复：
- 远端宿主机如果地址不是 `127.0.0.1` / `localhost`，不应被保存为 `auth_type=local`
- 当前后端已强制修正这一点
- 新增宿主机时默认认证方式应为 `password`

### 5.2 容器分配

平台支持：
- 指定端口创建容器
- 记录使用者、用途、密码
- CPU / 内存 / workspace 配额
- 默认挂载 `/mnt`
- 额外挂载可填，后续修改会标记为“需重建”

基础镜像默认值：
- `pytorch:2.7.1-cuda12.8-cudnn9-devel`

### 5.3 X11 forwarding

创建容器时会自动尝试补齐：
- `xauth`
- `x11-apps`
- `sshd_config` 中的：
  - `X11Forwarding yes`
  - `X11UseLocalhost yes`
  - `AllowTcpForwarding yes`

注意：
- 当前逻辑假设镜像是 Ubuntu/Debian 系，且可用 `apt-get`
- 客户端仍需自己具备 X server
- 用户需使用 `ssh -X` 或 `ssh -Y`

### 5.4 失败清场机制

这是当前平台的重要特性，已专门修过：

- 创建失败后自动清理已生成容器
- 删除动作做成幂等
- 启动 / 停止失败后会对账宿主机实际 Docker 状态并回写数据库
- 重建失败后会再次清掉失败容器
- 如果历史失败留下“同名残留容器”，下次创建前会尝试自动清掉

目标是尽量避免：
- 脏容器
- 脏端口
- 脏数据库状态
- 下一次创建持续性失败

### 5.5 快照

支持：
- 宿主机级默认快照策略
- 容器级独立快照策略
- 手动环境快照
- 自动保留份数轮转

当前快照实现：
- `docker commit`
- `docker save`

快照默认归档路径示例：
- `/mnt/docker_platform_snapshots/<host_slug>/<port>/`

### 5.6 远端已有容器展示

这是刚补过的能力。

现在宿主机详情页分成两类容器：
- 平台登记过的容器
- 宿主机实际存在、但未纳入平台登记的容器

后者当前为只读展示，显示：
- 容器名
- 镜像
- 端口
- 状态
- CPU 占用
- 内存占用
- 显存占用

这部分依赖远端：
- `docker inspect`
- `docker ps -a`
- `docker stats --no-stream`

### 5.7 移除宿主机记录

已支持从平台删除宿主机记录：
- 总览页远端宿主机卡片有“移除记录”
- 宿主机列表页也有“移除记录”

删除规则：
- 只删除平台内记录
- 不删除远端服务器
- 如果该宿主机仍有未删除 allocation，平台会阻止删除

## 6. 当前重要取舍与已知问题

### 6.1 `/hosts/{id}` 性能问题

远端宿主机详情页最初非常慢，实测：
- 修改前约 `50s`

已定位的主瓶颈：
- `used_host_ports()` 在远端大容器场景下极慢，曾单次耗时 `43.854s`

已做的降级：
- 宿主机详情页不再在首屏调用高成本的 `used_host_ports()`
- 暂时关闭了首屏的容器级 GPU 显存归属统计
- 暂时关闭了首屏 workspace 实时占用统计
- 暂时不在详情页使用 `docker stats` 反推平台登记容器的精细实时图表，只保留远端真实容器的只读资源展示

当前目标是优先“能打开、能看”，而不是首屏极度精细。

### 6.2 总览页仍可能偏慢

总览页 `/` 当前仍会遍历所有宿主机做 `host_summary()`。
当远端宿主机较多、容器较多时，首页仍有变慢风险。

后续建议优化方向：
- 宿主机卡片做缓存
- GPU 与 Docker 汇总异步化
- 首页只展示 reachability 和简化总览，重数据放到详情页按需加载

### 6.3 宿主机实际容器与平台分配容器的关系

当前“宿主机实际容器”只是只读展示，还没有“纳入平台管理”按钮。
如果迁移后继续建设，建议优先补：
- 将现有容器补登记为 allocation
- 或至少做“导入到平台”

## 7. 关键路由

### 页面
- `GET /`
- `GET /hosts`
- `GET /hosts/{host_id}`

### 宿主机
- `POST /hosts`
- `POST /hosts/{host_id}/snapshot-policy`
- `POST /hosts/{host_id}/delete`

### 容器分配
- `POST /allocations`
- `POST /allocations/{allocation_id}/resources`
- `POST /allocations/{allocation_id}/mounts`
- `POST /allocations/{allocation_id}/action`
- `POST /allocations/{allocation_id}/snapshot`
- `POST /allocations/{allocation_id}/restore/{snapshot_id}`

## 8. 当前前端交互特点

- 创建容器走 AJAX
- 维护动作走 AJAX
- 成功 / 失败都通过 modal 弹窗反馈
- 错误日志支持展示与复制
- 快照操作带进度条

## 9. 迁移到 222.201.144.165 的建议步骤

建议另一台 Codex 在目标机上按以下顺序做：

1. 进入目标目录
   - `/workspace/Control/`

2. 同步源码
   - 将 `/workspace/control/` 全量复制过去

3. 使用目标机现有 conda
   - 直接使用 `/opt/conda`
   - 不额外新建 `control` 环境

4. 安装依赖
   - 先 `pip install -r requirements.txt`
   - 再按本文件中的 `pip freeze` 补齐

5. 启动并初始化数据库
   - `python run.py`
   - 首次启动会自动创建 SQLite 表

6. 接入宿主机
   - 平台本机不一定需要作为核心宿主机
   - 重点是配置远端宿主机记录

7. 验证远端 SSHRunner 路径
   - 确保不是错误走 `local`
   - 确保 `docker info`、`docker ps`、`docker stats` 可运行

8. 验证 X11 创建链路
   - 可用测试端口创建一次临时容器
   - 验证后删除

9. 验证失败清场
   - 用错误镜像或故意失败场景测试是否会残留 Docker 脏状态

## 10. 目标服务器上的 Codex 应优先知道的坑

- 不能把远端宿主机保存成 `local`
- `allocations.container_name` 唯一，删除后要复用旧记录
- 远端详情页一旦直接扫完整端口或完整归属统计，会非常慢
- 平台的真实控制核心应是 SSHRunner，而不是 LocalRunner
- 失败清场逻辑不要回退

## 11. 本次迁移建议

用户明确希望：
- 平台本身部署在新的独立服务器上
- 通过 SSH 控制其他宿主机
- “本地直控”不是核心路径

因此目标机上的下一步重构建议是：
- 将本机自动注册宿主机改为可选，而不是默认核心
- 首页默认更强调“受管远端宿主机”
- 将本机宿主机能力降级为普通可选节点

## 12. 可直接复现的启动方式

```bash
conda activate base
cd /workspace/Control
python run.py
```

默认监听：
- `0.0.0.0:8080`

如果出现：
- `address already in use`

说明已有平台实例在占用 `8080`，先清理：

```bash
fuser -k 8080/tcp
python run.py
```

## 13. 建议另一台 Codex 的首要任务

如果由另一台 Codex 接手，建议按此优先级：

1. 在目标服务器完整复现当前版本
2. 让远端 SSH 控制链路稳定可用
3. 保留失败清场逻辑
4. 继续优化 `/hosts/{id}` 性能
5. 实现“宿主机实际容器纳入平台管理”
