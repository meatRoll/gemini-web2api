# 图片输入隔离测试

此测试使用模块版服务。附件格式、页面会话参数和 Chrome 传输移植自
[上游 PR #80](https://github.com/Sophomoresty/gemini-web2api/pull/80)，保留本 fork 的动态模型选择、
请求预算和 SSE 心跳。图片输入的 SSE 内容在识别完成后一次返回。

本目录的 Compose 配置需要支持 Docker 的 Linux 服务器。Serv00/FreeBSD 请使用
[原生图片部署包](../serv00/README.md)，其中包含已编译的 `curl_cffi`、Chrome 传输库和离线依赖，
不需要 Docker。本目录的 `smoke.py` 同样适用于 FreeBSD。

测试镜像通过当前分支的 Docker workflow 手动构建；分支镜像标签为
`ghcr.io/meatroll/gemini-web2api:feat-multimodal-image-input`，同时提供 `sha-<7位提交号>` 标签。
验收时优先使用提交标签或 digest，避免分支标签更新后测试版本不一致。

在 Linux 服务器上将本目录保存到单独位置，配置已有的私有认证导出文件：

```bash
cd deploy/image-test
export GEMINI_TEST_AUTH_FILE=/absolute/private/path/gemini-auth.json
# 可选：复用同一账号已有的模型快照，不会发起模型发现请求。
export GEMINI_TEST_MODEL_SNAPSHOT_FILE=/absolute/private/path/gemini-models.json
# 建议指定构建产出的不可变 digest。
export GEMINI_TEST_IMAGE=ghcr.io/meatroll/gemini-web2api:feat-multimodal-image-input
docker compose -f compose.yml pull
docker compose -f compose.yml up -d
```

认证和模型快照只读挂载；容器只监听服务器的 `127.0.0.1:18081`，通过 SSH 隧道访问。
如不设置模型快照，使用内置模型列表。启动测试实例不会访问 Google：模型检查和 Cookie 轮换
均关闭。没有周期性健康探测或自动生成请求，生成重试次数固定为 1，自动上下文缓存关闭。
容器不自动重启，限制 256 MiB 内存和 0.5 CPU，不与正式实例共享可写数据。

先检查一次本地健康状态（此请求只读取本地状态，不调用 Gemini）：

```bash
curl --fail http://127.0.0.1:18081/
```

随后执行一次真实图片验收：

```bash
python3 smoke.py --model gemini-3.6-flash
```

脚本生成一张测试 PNG，提示词不包含图片答案。仅当模型正确识别图中的颜色、形状和左右顺序时
报告 `verified: true`。每次运行仅发送 1 次 Chat Completions 请求；不自动重试。
同一个地址在 60 秒内无法再次调用，失败也会记录冷却时间。首次失败后停止，先检查服务日志，
不要循环调用脚本。输入一张图片通常还会产生 2 次页面读取、2 次上传 HTTP 请求和 1 次生成请求；
这些是同一次图片识别的协议步骤，不是 5 次推理。

需要验证 SSE 时，在首次验证成功且至少间隔 60 秒后，手动再执行一次：

```bash
python3 smoke.py --model gemini-3.6-flash --stream
```

两种模式合计最多 2 次生成。遇到 401、403、429、BardErrorInfo、空响应或识别失败，停止后续调用。
本地与 CI 单元测试均使用模拟响应，不访问 Gemini。

公网图片 URL 必须是 HTTPS，下载限制 10 MiB，校验文件签名、DNS 和每次重定向。
远程图片下载为保证 DNS 校验有效而绕过代理；需要通过代理读取图片时，优先使用 base64 data URL。
Gemini 页面、上传和生成仍支持配置的代理。

结束后停止独立实例：

```bash
docker compose -f compose.yml down
```
