# Third-party components

The MIT license in this repository applies to this project's own files only.
Third-party dependencies, container images and services retain their own terms.
This list covers direct dependencies, not every transitive package in the images.

| Component | Use | License and source |
| --- | --- | --- |
| DeepSeek Harness Python SDK and runtime | Conversational runtime | [MIT](https://github.com/deepseek-ai/deepseek-harness/blob/master/LICENSE); [SDK 0.2.2rc2](https://pypi.org/project/deepseek-harness-sdk/0.2.2rc2/) and [runtime 0.2.2rc2](https://pypi.org/project/deepseek-harness-runtime-bin/0.2.2rc2/) |
| DeepSeek plugin service packages, Cordis and Schemastery | Plugin runtime dependencies, pinned in package-lock.json | MIT; license files are included with each installed npm package |
| Tencent dsh-qqbot 0.5.5 | Official Harness QQ plugin; build-time bridge hook added by this project | [MIT](https://github.com/tencent-connect/dsh-qqbot/blob/main/LICENSE) |
| Tencent qqbot-nodejs 1.0.5 | QQ protocol, credentials and Gateway | [MIT](https://github.com/tencent-connect/qqbot-nodejs/blob/main/LICENSE) |
| websockets | Local mock Gateway in integration tests | [BSD-3-Clause](https://pypi.org/project/websockets/) |
| tzdata | Time zone data | [Apache-2.0](https://pypi.org/project/tzdata/) |
| Node.js base image | Plugin dependency build stage | [Node image](https://hub.docker.com/_/node); components have their own licenses |
| Python base image | Container runtime | [Python image](https://hub.docker.com/_/python); components have their own licenses |

QQ Bot API and the DeepSeek API are third-party services subject to their own
terms. This project integrates Tencent’s plugin through a small business adapter; it is
not an official Tencent product. See the [QQ Open Platform](https://q.qq.com/) and
[QQ Bot API documentation](https://bot.q.qq.com/wiki/develop/api-v2/).
