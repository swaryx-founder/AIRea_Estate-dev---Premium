# Real Estate pipeline — Build → Test → Deploy Staging

## Your plan (supported)

```
Pipeline → swaryxacrdev.azurecr.io/outboundai:<BuildId>
        → deploy realestate-dev (staging) + Library Swaryx-realestate

You (manual) → import same image → swaryxacrprod.azurecr.io/outboundai:<BuildId>
            → update production container yourself
```

Pipeline never pushes to prod ACR and never updates PROD.

## Stages

1. **Build** — push to `swaryxacrdev` only  
2. **Test** — CI smoke tests  
3. **Deploy Staging** — `realestate-dev` + `Swaryx-realestate` (regular job, no ADO Environment)

## Manual PROD after staging OK

```bash
# Example: import tag from DEV ACR into PROD ACR (run yourself)
az acr import \
  --name swaryxacrprod \
  --source swaryxacrdev.azurecr.io/outboundai:<BuildId> \
  --image outboundai:<BuildId>
```

Then point `swaryx-realestate` at `swaryxacrprod.azurecr.io/outboundai:<BuildId>`.
