# Deploying an instance with the Azure CLI

This guide sets up a complete instance of the decision engine in an Azure subscription, for us or for a client. Run the steps in order. Each step says whether it has been run on our own instance or is still untested.

When you're done, you'll have:

| Resource | Purpose | Step |
|---|---|---|
| Resource group | Holds everything except the Foundry resource, if that already exists | 3 |
| Foundry resource and project | Hosts the model, the agent, its conversations and the document vector store | 4 |
| Model deployment | The model the agent runs on | 4 |
| Agent (prompt agent) | Registered by the app on its first chat, not by hand | 8 |
| Vector store `he-programme-documents` | The programme documents the agent searches | 7 |
| Container App, its environment, registry and Log Analytics workspace | Runs the app (FastAPI + map) | 9 |
| Entra app registration | Sign-in in front of the app | 11 |
| Function app and its storage account (optional) | Refreshes the documents daily | 12 |

## Before you start

**Tools:**
- Azure CLI 2.91 or later: `brew install azure-cli`
- The Container Apps CLI extension: `az extension add --name containerapp --upgrade`
- [uv](https://docs.astral.sh/uv/)
- Optional, for step 12 only: Azure Functions Core Tools, `brew tap azure/functions && brew install azure-functions-core-tools@4`

You don't need Docker. Step 9 builds the image in Azure.

**Permissions:**
- **In Azure:** Owner of the subscription. Alternatively, Contributor plus Role Based Access Control Administrator, because several steps assign roles and Contributor alone can't do that.
- **In the client's Entra ID tenant:** permission to create app registrations (the Application Developer role) for step 11. Company tenants often don't allow this. If not, the client's IT team creates the registration from the request text in step 11. Ask early, because it's the step most likely to hold things up.

## 0. Set the variables

Every later command uses these. Change them for each instance and keep this block with the client's notes.

```sh
SUB="<subscription name or id>"
LOC=uksouth                              # keep the client's data in the UK
RG=rg-he-decision-engine                 # app resources
FOUNDRY_RG=rg-ai-applications            # Foundry resource; may be the same as $RG
FOUNDRY=ai-applications-uk-resource      # Foundry resource name, also its subdomain: globally unique
PROJECT=ai-applications-uk               # Foundry project name
MODEL=gpt-6-luna                         # model deployment name (the code calls it by this name)
AGENT=he-decision-engine                 # agent name in the Foundry project
APP=he-decision-engine                   # Container App name
APP_ENV=he-decision-engine-env           # Container Apps environment name

ENDPOINT=https://$FOUNDRY.services.ai.azure.com/api/projects/$PROJECT
```

The values shown are those of our own instance.

## 1. Sign in

```sh
az login
az account set --subscription "$SUB"
ME=$(az ad signed-in-user show --query id -o tsv)
```

## 2. Register the resource providers

*Run on our instance.* A new subscription often has none of these registered. A missing provider fails partway through a later step with `MissingSubscriptionRegistration`. Registration is free and takes a few minutes per provider.

```sh
for p in Microsoft.CognitiveServices Microsoft.App Microsoft.OperationalInsights \
         Microsoft.ContainerRegistry Microsoft.Storage Microsoft.Web; do
  az provider register --namespace $p --wait
done
```

`Microsoft.Storage` and `Microsoft.Web` are only needed for step 12, and for the planned move to Blob Storage.

## 3. Create the resource groups

```sh
az group create -n $RG -l $LOC
az group create -n $FOUNDRY_RG -l $LOC   # skip if the Foundry resource already exists
```

## 4. Create the Foundry resource, project and model deployment

*Not yet run from the CLI.* Our instance was created in the Foundry portal (ai.azure.com). These are the equivalent commands, with the settings copied from our resource. If the client already has a Foundry project, skip to the model deployment and use their names in step 0.

```sh
az cognitiveservices account create -n $FOUNDRY -g $FOUNDRY_RG -l $LOC \
  --kind AIServices --sku S0 --custom-domain $FOUNDRY \
  --allow-project-management --assign-identity --yes

az cognitiveservices account project create -n $FOUNDRY -g $FOUNDRY_RG -l $LOC --project-name $PROJECT

az cognitiveservices account deployment create -n $FOUNDRY -g $FOUNDRY_RG \
  --deployment-name $MODEL --model-name gpt-6-luna --model-version 2026-09-22 --model-format OpenAI \
  --sku-name GlobalStandard --sku-capacity 125
```

- **The project must be a Foundry project**, not a hub-based one ("Foundry classic"). The agent code uses the current Agent Service.
- **Check the deployment name.** `--deployment-name` is what the code calls the model, and it doesn't have to match the model's name. List what exists with `az cognitiveservices account deployment list -n $FOUNDRY -g $FOUNDRY_RG -o table`.
- **Data residency:** a `GlobalStandard` deployment can process prompts in any Azure region. If the client's data must stay in the UK, choose a regional SKU (`Standard`) for a model that offers one in UK South. Find one with `az cognitiveservices model list -l $LOC`.
- **Capacity** is in thousands of tokens per minute. One chat message can make up to 25 model calls and use about 90k tokens, so a low quota causes rate-limit errors partway through a message.

## 5. Give yourself access to Foundry

*Run on our instance.* Running the app or the document sync locally uses your own login, which needs the **Foundry User** role. This role used to be called Azure AI User, and the old name no longer works in `az role assignment create`.

```sh
FOUNDRY_ID=$(az cognitiveservices account show -n $FOUNDRY -g $FOUNDRY_RG --query id -o tsv)
az role assignment create --assignee-object-id $ME --assignee-principal-type User \
  --role "Foundry User" --scope $FOUNDRY_ID
```

Roles can take up to 10 minutes to apply.

## 6. Point the code at the instance

The code reads these environment variables. Its defaults are our own instance's values, so set them for any other instance.

| Variable | Used by | Value |
|---|---|---|
| `FOUNDRY_PROJECT_ENDPOINT` | `agent.py`, `refresh/docs_sync.py` | `$ENDPOINT` |
| `FOUNDRY_MODEL_DEPLOYMENT` | `agent.py` | `$MODEL` |
| `FOUNDRY_AGENT_NAME` | `agent.py` | `$AGENT` |

```sh
export FOUNDRY_PROJECT_ENDPOINT=$ENDPOINT FOUNDRY_MODEL_DEPLOYMENT=$MODEL FOUNDRY_AGENT_NAME=$AGENT
```

## 7. Load the programme documents

*Run on our instance.* This creates the vector store `he-programme-documents` and uploads the GOV.UK guidance listed in `SOURCES`. The app won't start a chat without the vector store.

```sh
uv run refresh/docs_sync.py
```

The first run reports each document as `added`. A second run should report them all as `unchanged`.

## 8. Test locally

*Run on our instance.*

```sh
uv run app.py
```

Open http://localhost:8000 and ask the agent "Best MSOA candidates for the BIL Fund". The first chat registers the agent in the Foundry project. The chat should show `file_search`, `describe_data`, `preview_config` and `apply_config` steps, then load a config into the map.

**Use one agent name per running copy of the app.** Each copy registers its own version of the agent when it starts. If a local copy and the deployed app share a name but run different code, they keep replacing each other's version. For local work after deployment, use `export FOUNDRY_AGENT_NAME=$AGENT-dev`.

## 9. Deploy the Container App

*Run on our instance.* Run this from the repo root. It creates a container registry, builds the `Dockerfile` in Azure, creates the environment and the app, and prints the app's URL. The first run takes several minutes.

```sh
az containerapp up -n $APP -g $RG -l $LOC --environment $APP_ENV \
  --source . --ingress external --target-port 8000 \
  --env-vars FOUNDRY_PROJECT_ENDPOINT=$ENDPOINT FOUNDRY_MODEL_DEPLOYMENT=$MODEL FOUNDRY_AGENT_NAME=$AGENT
```

To redeploy after code changes, run the same command again. It keeps the identity and sign-in settings.

The defaults are 0.5 CPU and 1 GiB, and the app scales to zero when idle:
- **If the app is killed for running out of memory,** add `--cpu 1 --memory 2Gi`.
- **To avoid a slow first visit,** add `--min-replicas 1`. This keeps one copy running and costs a little more.

## 10. Give the app access to Foundry

*Run on our instance.* In Azure, the app's managed identity takes the place of your `az login`. No code change is needed, because `DefaultAzureCredential` finds the identity automatically.

```sh
az containerapp identity assign -n $APP -g $RG --system-assigned
APP_ID=$(az containerapp show -n $APP -g $RG --query identity.principalId -o tsv)
az role assignment create --assignee-object-id $APP_ID --assignee-principal-type ServicePrincipal \
  --role "Foundry User" --scope $FOUNDRY_ID
```

Until the role applies (up to 10 minutes), chats fail with 401 or 403 errors.

To check:
1. Open the URL from step 9.
2. Ask the BIL Fund question from step 8.
3. If something fails, follow the logs with `az containerapp logs show -n $APP -g $RG --follow`.

## 11. Require sign-in

*In progress on our instance.* Waiting on an app registration from IT.

Until this step is done, anyone with the URL can use the app and the model quota. If you need a stopgap, restrict access to the office or VPN IP address with `az containerapp ingress access-restriction set`.

**If you can create app registrations:**
1. In the Azure portal, open the Container App, then **Authentication**, **Add identity provider**, and choose **Microsoft**.
2. Choose **Create new app registration**, with these settings:
   - **Supported account types:** Current tenant – Single tenant
   - **Restrict access:** Require authentication
   - **Unauthenticated requests:** HTTP 302 Found redirect

**If you can't,** send this to the client's IT or identity team. Get the app's hostname with `az containerapp show -n $APP -g $RG --query properties.configuration.ingress.fqdn -o tsv`.

> Please create an app registration in Entra ID for an Azure Container App's built-in sign-in:
> - **Name:** he-decision-engine
> - **Supported account types:** Single tenant (this directory only)
> - **Redirect URI (Web):** `https://<app hostname>/.auth/login/aad/callback`
> - **Authentication:** enable ID tokens
> - **API permissions:** Microsoft Graph `User.Read` (delegated), with admin consent
> - **A client secret**
>
> Please send me the Application (client) ID and the client secret.

Then add the Microsoft identity provider as above, but choose **Provide the details of an existing app registration**. Fill in:
- **Application (client) ID** and **Client secret:** from IT
- **Issuer URL:** `https://login.microsoftonline.com/<tenant id>/v2.0`. Get the tenant ID with `az account show --query tenantId -o tsv`.

Leave **Allowed token audiences** empty, and use the same restrict-access settings as above.

## 12. Optional: daily document refresh

*Not yet run.* `refresh/` is an Azure Function that runs `docs_sync.py` every day at 06:00 UTC, so the agent picks up GOV.UK changes without anyone running step 7 again. The Function gets its own storage account, which Functions need for their own bookkeeping.

```sh
ST=hedocsrefresh$RANDOM                  # storage account names: globally unique, 3–24 lowercase letters and digits
az storage account create -n $ST -g $RG -l $LOC --sku Standard_LRS
az functionapp create -n $APP-docs-refresh -g $RG --storage-account $ST \
  --flexconsumption-location $LOC --runtime python --runtime-version 3.12
az functionapp config appsettings set -n $APP-docs-refresh -g $RG --settings FOUNDRY_PROJECT_ENDPOINT=$ENDPOINT

az functionapp identity assign -n $APP-docs-refresh -g $RG
FUNC_ID=$(az functionapp identity show -n $APP-docs-refresh -g $RG --query principalId -o tsv)
az role assignment create --assignee-object-id $FUNC_ID --assignee-principal-type ServicePrincipal \
  --role "Foundry User" --scope $FOUNDRY_ID

(cd refresh && func azure functionapp publish $APP-docs-refresh)
```

To check:
1. In the portal, open the function app, then the `refresh_documents` function, and click **Test/Run**.
2. Check the log. A run with no changes logs `unchanged` for each document.

## Keeping an instance up to date

| Change | What to run |
|---|---|
| App code | Step 9 again. If the agent's instructions or tools changed, the first chat registers a new agent version. |
| Programme documents (a new entry in `SOURCES`, or a GOV.UK update) | `uv run refresh/docs_sync.py` (step 7), or wait for the daily Function if it's deployed. Only changed documents are uploaded again. |
| Data tables | `uv run prepare_data.py`, then step 9. The tables are built into the image. |

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `MissingSubscriptionRegistration` … `Microsoft.ContainerRegistry` (or another namespace) | Resource provider not registered | Step 2, then rerun the failed command; it reuses what was already created |
| `DeploymentNotFound` from a chat | `FOUNDRY_MODEL_DEPLOYMENT` doesn't match a deployment name | List deployments (step 4) and fix the variable |
| `No vector store 'he-programme-documents'` | Step 7 not run in this Foundry project | Step 7 |
| 401 or 403 from Foundry, or `AuthorizationPermissionMismatch` | Role missing or not yet applied | Steps 5, 10 or 12, then wait up to 10 minutes |
| `Role 'Azure AI User' doesn't exist` | The role was renamed | Use `Foundry User` |
| The agent keeps changing between versions | Two copies of the app with different code share an agent name | A separate `FOUNDRY_AGENT_NAME` per copy (step 8) |
| Saved configs disappear | `configs/` is on the container's disk, which is wiped on every restart or redeploy | Planned move to Blob Storage (below) |

## Not covered yet

These are planned but not yet built. The steps will be added here once they've been run:

- **Blob Storage:** data tables and saved configs, so new data doesn't need a redeploy and configs survive restarts.
- **`list_documents`:** brought back as a tool that lists the vector store's documents.
