# Third-party components

The MIT license in this repository applies to this project's own files only.
Third-party dependencies, container images and services retain their own terms.
This list covers direct dependencies, not every transitive package in the images.

| Component | Use | License and source |
| --- | --- | --- |
| DeepSeek Harness Python SDK and runtime | Conversational runtime | [MIT](https://github.com/deepseek-ai/deepseek-harness/blob/master/LICENSE); [SDK 0.2.2rc2](https://pypi.org/project/deepseek-harness-sdk/0.2.2rc2/) and [runtime 0.2.2rc2](https://pypi.org/project/deepseek-harness-runtime-bin/0.2.2rc2/) |
| websockets | OneBot WebSocket client | [BSD-3-Clause](https://pypi.org/project/websockets/) |
| tzdata | Time zone data | [Apache-2.0](https://pypi.org/project/tzdata/) |
| Python base image | Container runtime | [Python image](https://hub.docker.com/_/python); components have their own licenses |
| NapCat Docker image | Separate QQ / OneBot service | [NapCatQQ](https://github.com/NapNeko/NapCatQQ/) and [image](https://hub.docker.com/r/mlikiowa/napcat-docker); check the terms for the exact version used |

This repository references the NapCat image; it does not contain its source code
or redistribute its image. Earlier published NapCat terms include nonstandard
restrictions on modification, redistribution and commercial use. The upstream
license file could not be retrieved during this review, so the terms for the
exact image version and their effect on this separate OneBot client are not
established here. Check the applicable version and obtain clarification from
the rights holder where necessary before distributing or offering a service.
See the [NapCat project](https://github.com/NapNeko/NapCatQQ/) for its current
information. QQ and the DeepSeek API are third-party services subject to their
own terms.
