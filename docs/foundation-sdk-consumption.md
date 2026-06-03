# foundation-sdk 安装路径

业务服务（bff / agent-scheduler / session-orchestrator / sandbox-pool）需要
`foundation_sdk` 来调 SaaS Foundation 服务和复用 FastAPI 中间件。
有两条获取路径，按场景选择：

## 路径 A：内部 PyPI（默认，CI / 生产）

`pyproject.toml` 中已声明：

```toml
foundation-sdk = { version = ">=0.1,<0.2", extras = ["fastapi"], source = "internal-pypi" }

[[tool.poetry.source]]
name = "internal-pypi"
url = "https://pypi.internal.example/simple/"
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
在 `setup-service/action.yml` 把现在的 `pip install -e .../foundation/...`
那一步替换为：

```yaml
- shell: bash
  env:
    POETRY_HTTP_BASIC_INTERNAL_PYPI_USERNAME: __token__
    POETRY_HTTP_BASIC_INTERNAL_PYPI_PASSWORD: ${{ secrets.INTERNAL_PYPI_TOKEN }}
  run: poetry install --no-interaction
```

## 路径 B：docker-compose build-context（dev 兜底）

`deploy/docker-compose/agent-platform.yml` 仍保留：

```yaml
additional_contexts:
  foundation_sdk: ${FOUNDATION_SDK_PATH:-../../../saas-foundation/sdks/python}
```

各服务 `Dockerfile` 里的 `COPY --from=foundation_sdk . /sdk` +
`pip install -e /sdk` 也照旧。这条路径在以下场景仍有价值：

1. **离线开发** —— 笔记本没法连内部 PyPI；
2. **SDK 改动还没发版** —— 在 saas-foundation 改 SDK，想在 agent-platform
   起的容器里立刻看到效果，不想为了试一次改动走一次完整发布；
3. **本地排错** —— 想在 SDK 代码里临时打 print/breakpoint。

> 这条路径会**覆盖** `pip install foundation-sdk` 装出来的版本（`pip install -e`
> 后置），所以 dev 模式拿到的永远是工作区当前代码，而不是 pyproject 里 pin 的版本。

## 怎么决定用哪条？

| 场景                                 | 路径 |
| ------------------------------------ | ---- |
| CI / 镜像构建 / 生产部署             | A    |
| 跨仓库联调 SDK + 业务（本机）        | B    |
| 业务开发但 SDK 不动                  | 任意一条都行；推荐 A，少一个变量 |
| Demo 给外部人员（无 VPN）            | B    |

## 升级 foundation-sdk

minor 之内（`0.1.x -> 0.1.y` / `0.1.x -> 0.2.0`）：

```bash
cd services/<svc>
poetry update foundation-sdk
git diff poetry.lock   # review，跑测试
```

major（`0.x -> 1.x`）：**必须**等 saas-foundation 那侧的 ADR 决定后，
同 PR 改 `pyproject.toml` 把 pin 改到 `>=1.0,<2.0`，再 `poetry lock`。
详见 `saas-foundation/docs/sdk-release.md` 第 1 节。
