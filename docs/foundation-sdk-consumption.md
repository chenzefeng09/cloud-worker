# foundation-sdk 安装路径

## 谁在用、用到了什么

8 个服务 import `foundation_sdk`：bff、session-orchestrator、agent-scheduler、
sandbox-pool、app-registry、mcp-registry、skill-store、knowledge-ingest。
它们只用到下面这些名字，没有服务用 `FoundationClient` 或任何 resource 客户端：

* `foundation_sdk`：`CookieSigner`、`CookieSignerSettings`、`SignatureExpiredError`、
  `SignatureInvalid`（只有 bff 用）；
* `foundation_sdk.fastapi`：`install_request_context`、`register_problem_details`、
  `get_tenant_id`、`get_user_id`、`get_request_id`、`get_clearance`、`push_identity`。

升级 SDK 时要核对的就是这份清单，见最后一节。

## 现状：没有任何地方 pin 版本

没有一个服务在 `pyproject.toml` 里声明 `foundation-sdk`（几份 pyproject 里只有
一段说明它从哪来的注释），`poetry.lock` 里也没有它，也没有 `[[tool.poetry.source]]`。
各处装的都是某个 saas-foundation 源码树里的 `sdks/python`，SDK 的版本号不约束任何东西：

| 场景                | SDK 从哪来                                                                                                                         | 实际装上的是                                                         |
| ------------------- | ---------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| docker-compose 镜像 | `foundation_sdk` build context（路径 B）                                                                                           | 上次构建 session-orchestrator 镜像时 `FOUNDATION_SDK_PATH` 里的代码 |
| CI                  | `.github/actions/setup-service` checkout saas-foundation 的 `foundation-ref`（默认 `main`），`poetry install` 之后 `pip install -e` | saas-foundation `main` 的最新提交                                    |
| 本机 venv           | `make install-sdk`：`pip install -e $(FOUNDATION_DIR)/sdks/python[fastapi]`                                                        | 本机 saas-foundation checkout 当前的工作区                           |

所以 SDK 的改动一合进 saas-foundation 的 `main`，下一次 CI 就会用上；本地镜像
在下一次重建时用上。

## 路径 A：内部 PyPI（目标，还没接通）

等内部 PyPI 能提供 `foundation-sdk`，在上面 8 个服务的 `pyproject.toml` 里声明：

```toml
foundation-sdk = { version = ">=1.0,<2.0", extras = ["fastapi"], source = "internal-pypi" }

[[tool.poetry.source]]
name = "internal-pypi"
url = "https://pypi.example.com/simple/"
priority = "supplemental"
```

`priority = "supplemental"` 表示：默认从公网 PyPI 拉所有包，**只有显式
绑定到 `internal-pypi` 的包**（即 foundation-sdk）才走内部源。这样不影响
其他依赖的拉取速度。

CI 中需要配置：

* GitHub Actions secret `INTERNAL_PYPI_URL` 透传给
  `actions/setup-python` 之后的 `poetry install` 步骤，或；
* 在 runner 上预置 `~/.config/pip/pip.conf` 写好 `extra-index-url`。

推荐方式（agent-platform CI 用 setup-service composite action）：
在 `setup-service/action.yml` 去掉 checkout saas-foundation 的那一步，并把现在的
`poetry install` + `pip install -e "$GITHUB_WORKSPACE/_foundation/sdks/python[fastapi]"`
那一步替换为：

```yaml
- shell: bash
  env:
    POETRY_HTTP_BASIC_INTERNAL_PYPI_USERNAME: __token__
    POETRY_HTTP_BASIC_INTERNAL_PYPI_PASSWORD: ${{ secrets.INTERNAL_PYPI_TOKEN }}
  run: poetry install --no-interaction
```

镜像要一起改：`Dockerfile.overlay` 每次构建都会用 build context 里的源码强制
重装 SDK（见路径 B），不改的话镜像里仍然是 context 里的那一份，pin 管不到。

## 路径 B：docker-compose build context（目前所有镜像都走这条）

`deploy/docker-compose/agent-platform.yml` 在 session-orchestrator 的构建里传入：

```yaml
additional_contexts:
  foundation_sdk: ${FOUNDATION_SDK_PATH:-../../../saas-foundation/sdks/python}
```

用到这个 context 的只有 session-orchestrator 的两个 Dockerfile：

* `Dockerfile`（构建 `agent-platform-dev-session-orchestrator:base`）：
  `COPY --from=foundation_sdk . /sdk`，在 `poetry install` 之前
  `pip install "/sdk[fastapi]"`；
* `Dockerfile.overlay`（构建 compose 里跑的 `agent-platform-dev-session-orchestrator`）：
  每次构建都从同一个 context 用 `pip install --no-deps --force-reinstall "/tmp/sdk[fastapi]"`
  重装一遍。

bff、agent-scheduler、sandbox-pool、app-registry、mcp-registry、skill-store、
knowledge-ingest 的 Dockerfile 都是 `FROM agent-platform-dev-session-orchestrator:latest`，
SDK 是从这个镜像继承来的。compose 给其中几个服务的构建也传了 `foundation_sdk`
context，但它们的 Dockerfile 没有用它。

两点要注意：

* 装的是快照，不是 `pip install -e`：改了 SDK 以后，要重建 session-orchestrator
  镜像，再重建 FROM 它的那些镜像，改动才会进到容器里。
* `FOUNDATION_SDK_PATH` 指向一个工作区，它当时在哪个分支，镜像里就是哪个分支的
  SDK，不一定是 saas-foundation 的 `main`，更不一定是哪个发布版本。

路径 A 接通以后，这条路径在以下场景仍有价值：

1. **离线开发** —— 笔记本没法连内部 PyPI；
2. **SDK 改动还没发版** —— 在 saas-foundation 改 SDK，想在 agent-platform
   起的容器里看到效果，不想为了试一次改动走一次完整发布；
3. **本地排错** —— 想在 SDK 代码里临时打 print/breakpoint。

## 怎么决定用哪条？

路径 A 接通之前，各场景都按"现状"一节的方式拿到 SDK。接通之后：

| 场景                                 | 路径 |
| ------------------------------------ | ---- |
| CI / 镜像构建 / 生产部署             | A    |
| 跨仓库联调 SDK + 业务（本机）        | B    |
| 业务开发但 SDK 不动                  | 任意一条都行；推荐 A，少一个变量 |
| Demo 给外部人员（无 VPN）            | B    |

## 升级 foundation-sdk

从 1.0.0 起 pin 是 `>=1.0,<2.0`：1.x 之内的 minor、patch 都在 pin 允许的范围里，
major 不在。（1.0.0 之前的 pin `>=0.1,<0.2` 只允许 0.1.x，0.2.0 也进不来。）

minor / patch（`1.0.x -> 1.0.y`、`1.0.x -> 1.1.0`）：不用改 pin。

```bash
cd services/<svc>
poetry update foundation-sdk
git diff poetry.lock   # review，跑测试
```

major（`1.x -> 2.0.0`）：`<2.0` 把它挡在外面，`poetry update` 不会升上去。
**必须**等 saas-foundation 那侧的 ADR 决定后，同一个 PR 里把 pin 改到
`>=2.0,<3.0`，再 `poetry lock`。详见 `saas-foundation/docs/sdk-release.md` 第 1 节。

路径 A 接通之前没有 pin 能挡住 major：SDK 合进 saas-foundation 的 `main`，下一次
CI 就会用上。所以 SDK 发 major 之前，要先在 agent-platform 这边验证：

1. 对照第一节的清单，看新版本有没有改这些名字；
2. 把 `PYTHONPATH` 指向新版本的 `sdks/python`，跑上面 8 个服务的测试。

0.1.0 -> 1.0.0（saas-foundation ADR 0001）就是这样验证的：1.0.0 改的是 resource
客户端、transport 和版本号，清单里的名字所在的 `_signing.py`、`_fastapi.py`、
`fastapi.py` 一行没变，agent-platform 不需要改代码。
