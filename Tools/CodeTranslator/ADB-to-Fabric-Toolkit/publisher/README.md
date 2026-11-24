# Notebook publisher

The publisher previews and optionally sends reviewed `.ipynb` notebooks from a converter output folder to a Microsoft Fabric workspace or a new Azure DevOps Git branch. It is separate from the converter; review and validate the conversion report and notebook code first.

## Prerequisites

* Python **3.9 or later**; the publisher uses the standard library.
* A normal converter output folder containing `.ipynb` files. A converter dry run does not produce notebooks.
* **Fabric:** an accessible test workspace, permission to create or update notebooks, and Azure CLI signed in with `az login` or an approved `FABRIC_TOKEN` environment variable. Workspace folder operations require appropriate permissions.
* **Azure DevOps:** Git, repository access and permission to create and push a branch. Git Credential Manager can authenticate Git; `ADO_PAT` is optional if permitted by your organization's policy. Creating a PR requires additional permissions and authentication.
* Windows for the provided `.bat` launcher and folder picker.

Never put credentials in the repository or configuration file. Use your organization's approved authentication and access controls.

## Run the publisher

There are two ways to run the publisher. Both preview first and write the same logs and reports.

### Option 1: Windows launcher (`.bat` file)

1. Double-click `Publish-Notebooks.bat` in the `publisher` folder, or drag the actual timestamped converter output folder onto it.
2. Choose the destination (Fabric workspace or Azure DevOps repository) and answer the prompts.
3. The launcher always runs a preview first and asks for confirmation before publishing.

### Option 2: Command line

Open PowerShell, go to the `publisher` folder and run a preview first, replacing the placeholders:

```powershell
cd C:\work\toolkit\publisher
python -m a2f_publish fabric "C:\work\converted_YYYYMMDDHHMM" --workspace-id "<workspace-guid>" --dry-run
python -m a2f_publish ado "C:\work\converted_YYYYMMDDHHMM" --repo-url "https://dev.azure.com/<org>/<project>/_git/<repo>" --base-branch main --dry-run
```

Review the preview and logs, then remove `--dry-run` to publish. **A real command-line run does not ask for confirmation.** For Fabric, `--folder "Migration\Notebooks"` selects a destination folder and `--on-exists skip|update|fail` controls existing notebooks; the default is `skip`. Update overwrites notebook content and may move it to the specified folder.

Alternatively, copy `publish.config.sample.json` to `publish.config.json`, edit the non-secret settings, and run:

```powershell
python -m a2f_publish --config publish.config.json
```

The configuration example defaults to a real run (`"dry_run": false`); set it to `true` for the preview. Run `python -m a2f_publish fabric --help` or `python -m a2f_publish ado --help` for all options.

## Outputs and limitations

Each run writes `_report\publish.log` and `_report\publish_fabric.json` or `_report\publish_ado.json` under the selected input folder. A dry run contacts the destination and writes local reports but does not publish notebooks or push changes.

* Notebooks containing `TODO[ADB2FABRIC]` are blocked by default. The launcher asks before including them; the CLI override is `--include-needs-manual`. Resolve the findings before execution.
* Azure DevOps publishing creates a new branch, not a direct change to the base branch. It writes plain `.ipynb` files, not Fabric Git integration's native notebook format.
* Created Fabric notebooks do not have a default lakehouse or environment attached. Configure required resources and test before promotion.
* Fabric workspace folder APIs are preview. Validate publishing in a test workspace first; live service operation is not established by local and mock tests.

## Troubleshooting

Check `publish.log` and the JSON report for each notebook's result. For Fabric authentication or 401 errors, check `az login`, token validity and workspace permissions. If nothing is published, check whether notebooks were blocked or existing items were skipped. Folder and per-notebook failures are reported; inspect any partial-update errors before retrying.
