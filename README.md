# k8schema

Dynamic OpenAPIv3 generator for your running Kubernetes installation; giving your language server support for Openshift, CRDs, and more.

I was frustrated that the [yaml-language-server](https://github.com/redhat-developer/yaml-language-server) and by proxy [helm_ls](https://github.com/mrjosh/helm-ls) don't provide suggestions for any CRDs. This schema server will scrape your Kubernetes installation to index all resource definitions and generate a language server compatible API endpoint.

Although primarily designed to work with language servers, with a little hacking you can get CDK8s to also use this API.

## Running the server

```bash
pip install .
python -m k8schema
```

This will start a server listening on 0.0.0.0:8000. Your Kubernetes credentials are automatically read from `~/.kube/Config`.

## Usage with Language Servers

### yaml-language-server

The yaml-language-server includes a built-in `kubernetes` schema but this only covers the core Kubernetes RDs and at the time of writing is fixed to version 1.22.4. Update `schemas` in the yamlls config to point to the running API server.

#### Neovim

```lua
schemas = {
    ["http://127.0.0.1:8000/all.json"] = "*",
},

```

### helm-ls

Helm-ls defers YAML processing to yaml-language-server so can be configured in a similar way

#### Neovim

```lua
settings = {
    ['helm-ls'] = {
        yamlls = {
            enabled = true,
            diagnosticsLimit = 50,
            showDiagnosticsDirectly = false,
            path = "yaml-language-server",
            config = {
                schemas = {
                    ["http://127.0.0.1:8000/all.json"] = "templates/**",
                },
                completion = true,
                hover = true,
            }
        },
    },
},

```

## Usage with CDK8s

Sadly, right now this process is more involved. There is no way to configure the URL that CDK8s uses for the k8s schemas ([issue here](https://github.com/cdk8s-team/cdk8s-cli/issues/1938)). You can modify the `cdk8s-cli` package in `node_modules/cdk8s-cli/lib/import/k8s.js`

Change

```javascript
async function downloadSchema(apiVersion) {
    const url = `https://raw.githubusercontent.com/cdk8s-team/cdk8s/master/kubernetes-schemas/v${apiVersion}/_definitions.json`;
    let output;
```

to

```javascript
async function downloadSchema(apiVersion) {
    const url = `http://127.0.0.1:8000/_definitions.json`;
    let output;
```

Now run

```bash
cdk8s import k8s
```

and you'll have a set of files built from your Kubernetes installation.

## Known Limitations

### Authentication

I've been developing this predominantly against MiniKube so its quite possible this doesn't authenticate correctly against your Kubernetes deployment, or that my `KubeConfig` parser fails. Please feel free to open an issue.

Currently supported authentication mechanisms
* mTLS client certificate
* Bearer token

### Tekton

Tekton doesn't provide OpenAPIv3 Schemas for their CRDs so you won't get completion for `Pipeline` or `Task` sadly - there is an open [issue](https://github.com/tektoncd/pipeline/issues/1461)
