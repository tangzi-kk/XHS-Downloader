# 小红书实况图地址提取修复日志

- 日期：2026-08-01
- 仓库：`tangzi-kk/XHS-Downloader`
- 分支：`master`
- 状态：代码已提交；Render 生产环境最终回归待确认

## 1. 问题现象

调用 `/xhs/detail` 解析一条包含 17 张实况照片的小红书图文作品时：

- 普通图片地址能够正常返回；
- `动图地址` 返回 17 个 `null`；
- 作品被正确识别为图文，不属于普通视频解析路径。

这说明图文主体解析正常，故障集中在实况视频地址提取阶段。

## 2. 根因

旧实现只读取：

```text
stream.h264[0].masterUrl
```

生产环境诊断确认，该作品当前真实字段为：

```text
imageList[n].stream.EF4[0].masterUrl
imageList[n].stream.EF4[0].backupUrls
```

诊断结果共识别：

- 17 张图片；
- 每张图片 1 个 `masterUrl`；
- 每张图片 2 个 `backupUrls`；
- 合计 51 个疑似视频地址。

因此，原逻辑并非没有拿到实况数据，而是字段路径已经与旧实现不一致。

## 3. 诊断过程

为避免直接猜测字段，增加了只读诊断接口：

```text
POST /xhs/debug/live-photo
```

诊断接口：

- 复用现有短链解析、Cookie 和网页数据解析逻辑；
- 只检查 `imageList`；
- 返回相关字段路径和脱敏后的候选地址；
- 不返回 Cookie、请求头、完整原始 JSON 或 URL 查询参数；
- 使用 `X-Debug-Token` 请求头进行鉴权。

相关提交：

```text
046134bb42a3445f1fb36b55b51facb22077c5c1
779543dfaf56e93dd7d2c8a381acfbe1027d46eb
318566a817e76b430eac49191fb93bdf64609c04
a360049ff1ebf859d1e2c4fd162204a912fe086f
43f1bcda99a14605a4fe3c8c35a3635ccb750f1d
```

## 4. 修复内容

修改文件：

```text
source/application/image.py
```

新的实况地址提取顺序：

```text
stream.EF4[0].masterUrl
stream.EF4[0].backupUrls[0]
stream.h264[0].masterUrl
stream.h264[0].backupUrls[0]
stream.h265[0].masterUrl
stream.h265[0].backupUrls[0]
```

已知路径全部缺失时，会继续遍历 `stream` 下的列表字段，寻找：

```text
masterUrl
backupUrls[0]
```

兼容策略：

- 保持图片原有索引顺序；
- 一张图片只返回一个首选实况地址；
- 没有实况数据的图片继续返回 `None`；
- 不修改普通图片生成逻辑；
- 不修改普通视频解析逻辑；
- 不修改 Coze、飞书和任务队列流程。

代码提交：

```text
a719d4d6cc6e925095590c22beb814b272cc6224
```

## 5. 测试覆盖

新增文件：

```text
tests/test_image_live_photo.py
```

覆盖场景：

- 当前生产字段 `stream.EF4`；
- 旧版字段 `stream.h264`；
- 主地址缺失时使用 `backupUrls[0]`；
- 未知字段名，例如 `stream.EF9`；
- 非实况图片保持 `None`；
- 多张图片的返回顺序保持一致。

测试提交：

```text
24398702f81a5a434c481a351a38f18089a7f651
```

截至记录时，GitHub 未返回 CI 状态，因此不能将“测试文件已提交”等同于“云端测试已通过”。

## 6. 验收标准

Render 部署最新 `master` 后，使用原作品重新调用：

```text
POST /xhs/detail
```

预期结果：

- 返回 17 个普通图片地址；
- 返回 17 个非空实况视频地址；
- 每个实况地址对应原图片索引；
- 普通图文和普通视频作品行为不受影响。

## 7. 风险与后续观察

当前兼容未知 `stream` 字段的逻辑采用“找到第一个可用 `masterUrl` 或 `backupUrls[0]`”的策略。

仍需观察：

- 小红书是否继续调整 `stream` 内部字段；
- 同一图片是否可能同时出现多种不同用途的视频流；
- Render 生产环境是否能稳定访问返回的 MP4 地址；
- `http` 视频地址在后续下载或飞书上传链路中是否需要统一转换为 `https`。

## 8. 回退方式

如新逻辑导致异常，可回退代码提交：

```text
a719d4d6cc6e925095590c22beb814b272cc6224
```

回退后将恢复为只读取：

```text
stream.h264[0].masterUrl
```

但当前 `EF4` 类型实况作品会再次返回空地址。

## 9. 安全说明

本日志不记录任何真实 Cookie、访问 Token、请求头凭据或带查询参数的完整作品地址。曾在调试过程中暴露的 Token 应在对应平台作废并重新生成。
