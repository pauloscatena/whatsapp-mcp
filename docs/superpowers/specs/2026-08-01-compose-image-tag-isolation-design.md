# Isolamento de tags de imagem no docker-compose (whatsapp-bridge vs whatsapp-mcp)

- **Repo:** whatsapp-mcp
- **Branch:** `docker-container-setup` (HEAD `3a7d9bd`)
- **Data:** 2026-08-01
- **Status:** aprovado, pendente de aplicação

## Contexto e incidente

O `docker-compose.yml` declara dois serviços, `whatsapp-bridge` e `whatsapp-mcp`. Ambos:

- usam `build: .` (o mesmo Dockerfile na raiz do repo);
- usam `image: whatsapp-mcp:latest` (a mesma tag).

Eles diferem apenas em `command:` (`["bridge"]` vs `["mcp"]`), despachado pelo `entrypoint.sh`. O serviço `whatsapp-mcp` ainda tem `depends_on: whatsapp-bridge: condition: service_healthy`.

O serviço `whatsapp-bridge` é quem mantém a sessão autenticada do WhatsApp. Recriar o container dele arrisca forçar um novo login via QR code.

**Incidente observado em produção (2026-08-01):** rodar `docker compose up -d --build whatsapp-mcp` — um comando explicitamente restrito a UM serviço — reconstruiu a tag compartilhada e fez o Compose recriar TAMBÉM o `whatsapp-bridge`, trocando o ID do container e zerando o uptime dele. A sessão do WhatsApp sobreviveu apenas porque o volume nomeado `whatsapp-store` não foi tocado e o bridge releu o estado persistido (reconectou com "Successfully authenticated", sem QR). Isso é sorte estrutural, não uma garantia.

## Diagnóstico

Verificado empiricamente com `docker compose --dry-run` (Compose v5.3.1). Existem **dois mecanismos independentes** — este é o núcleo do problema, e cada um exige uma correção própria.

### Mecanismo 1 — Tag compartilhada

O Compose agrupa builds pelo nome de imagem resultante. Evidência:

```
docker compose --dry-run build whatsapp-mcp
```

Mesmo restrito a um serviço (e `build` não segue `depends_on`), o comando emitiu builds para OS DOIS serviços, cada um terminando em `naming to whatsapp-mcp:latest`.

Conclusão: a tag compartilhada, por si só, já faz um build de um serviço afetar o outro — todo build reaponta a tag que o outro serviço resolve.

### Mecanismo 2 — Escopo do `depends_on`

```
docker compose --dry-run up -d --build whatsapp-mcp
```

colocou o `whatsapp-bridge` inteiramente em escopo: ele foi **Built, Created e Started**. `up SERVICE` inclui os alvos de `depends_on` a menos que `--no-deps` seja passado, e `--build` constrói tudo que está em escopo.

Evidência de que `--no-deps` resolve isso:

```
docker compose --dry-run up -d --build --no-deps whatsapp-mcp
```

emitiu apenas o build do `whatsapp-mcp` e apenas o container do `whatsapp-mcp`.

### Conclusão crítica

Tags distintas **sozinhas não teriam evitado o incidente**, porque o Mecanismo 2 reconstrói e recria o bridge independentemente das tags. A correção tem, portanto, uma metade estrutural (tags) e uma metade procedural (`--no-deps` no comando documentado). **Cada metade é insuficiente isoladamente.**

## Decisão 1: esquema de tags

**Decidido:** só o `whatsapp-mcp` recebe uma tag nova.

| Serviço | `image:` antes | `image:` depois |
|---|---|---|
| `whatsapp-bridge` | `whatsapp-mcp:latest` | `whatsapp-mcp:latest` (inalterado, deliberadamente) |
| `whatsapp-mcp` | `whatsapp-mcp:latest` | `whatsapp-mcp-server:latest` |

**Racional:** alterar o campo `image:` de um serviço muda o hash de configuração daquele serviço, e o Compose recria o container correspondente uma vez — mesmo quando o conteúdo da imagem referenciada é byte-idêntico. Renomear o bridge custaria exatamente a recriação do bridge que este trabalho existe para eliminar. Renomear apenas o serviço mcp quebra o acoplamento com a mesma eficácia (o desacoplamento é simétrico — basta que um dos dois tenha tag própria) e garante que o container do bridge nunca é recriado, nem agora nem no rollout desta mudança.

**Mitigação rejeitada, registrada explicitamente:** pré-executar `docker tag whatsapp-mcp:latest whatsapp-bridge:latest` NÃO evita a recriação, porque o hash de configuração é calculado sobre o NOME da imagem, não sobre o ID de imagem resolvido.

**Custo aceito:** o serviço chamado `whatsapp-bridge` carrega uma tag de imagem chamada `whatsapp-mcp:latest`, o que lê como contraintuitivo. **Mitigação do custo:** um comentário no `docker-compose.yml`, exatamente naquela linha, explicando por que ela não deve ser "arrumada" — uma limpeza futura custaria silenciosamente uma recriação do bridge.

### Alternativas rejeitadas

| Alternativa | Por que foi rejeitada |
|---|---|
| Renomear os dois (`whatsapp-bridge:latest` + `whatsapp-mcp-server:latest`) | Nomenclatura mais limpa, mas força uma recriação única do bridge no rollout. |
| Dockerfile targets separados (imagem Go-only para o bridge, Python-only para o mcp) | Desproporcional. O healthcheck do bridge no `docker-compose.yml` executa `python3 -c ...` DENTRO do container do bridge — uma imagem Go-only quebraria esse healthcheck. Além disso, o `entrypoint.sh` existe justamente para permitir que uma única imagem sirva os dois papéis. |

## Decisão 2: comando documentado

Rebuild de serviço único, canônico:

```
docker compose up -d --build --no-deps whatsapp-mcp
```

As duas flags são obrigatórias e nenhuma é cosmética: `--no-deps` é a metade da correção que a mudança de tag não consegue fornecer sozinha.

Também deve ficar documentado: `docker compose up -d --build` (stack completa) reconstrói e recria o bridge, carregando risco de re-login por QR. As instruções de instalação atuais do README (passo 3) usam exatamente essa forma de stack completa — o que é correto para uma primeira instalação, mas não deve ser o caminho de rotina para atualizações.

## Fora de escopo e restrições (não alterar)

- O mapeamento `127.0.0.1:8081:8081` do `whatsapp-mcp`. Bind em loopback + túnel SSH é o ÚNICO controle de acesso do endpoint `/mcp`, que não tem autenticação própria.
- A ausência de qualquer `ports:` no `whatsapp-bridge` (um pentest autorizado confirmou que exposição na LAN ali seria explorável).
- O volume `whatsapp-store` (contém `messages.db` e o estado da sessão).
- O `depends_on` em si — continua correto para ordenação de cold start.
- Nenhum `docker compose down`, nenhum `-v`, nenhum prune em nenhum momento.

## Plano de validação

### Restrições locais que invalidam um teste ingênuo

Duas restrições descobertas no ambiente local tornam um `docker compose up` local ingênuo inválido como teste, e motivam o método escolhido abaixo:

1. Em `whatsapp-bridge/main.go`, `startRESTServer` é chamado na linha 1091, DEPOIS do bloco de login por QR nas linhas 1038–1066. O listener REST do bridge, portanto, nunca abre sem um login bem-sucedido no WhatsApp — o healthcheck dele nunca pode passar localmente, e `depends_on: service_healthy` bloquearia o `whatsapp-mcp` indefinidamente.
2. A porta 8081 do host já está ligada na máquina de desenvolvimento por um processo `ssh.exe` (o túnel do operador para produção). Ela não pode ser perturbada, e o mapeamento de porta não deve ser alterado para contornar isso.

### Método escolhido

Usar `docker compose create` (e `--dry-run`) em vez de `up`. `create` aplica a mesma lógica de recreate/config-hash mas nunca inicia um container — sem prompt de QR, sem bind de porta, sem interferência no túnel SSH. A propriedade sob teste é uma propriedade de orquestração do Compose, então é exercida por completo sem executar as aplicações.

### Cenário de teste

Rodado uma vez como CONTROLE contra o compose sem a correção (onde DEVE falhar) e depois novamente contra o compose corrigido (onde DEVE passar). Isso espelha a falha real "reconstruí o mcp, e um `up -d` de rotina posterior destruiu meu bridge":

1. `docker compose -p wa-test create` (cria os dois containers, não inicia nenhum); registrar o ID do container do bridge.
2. Alterar um arquivo sob `whatsapp-mcp-server/` para invalidar o cache de build.
3. `docker compose -p wa-test build whatsapp-mcp`.
4. `docker compose -p wa-test create` novamente; reler o ID do container do bridge.
5. ASSERT: o ID do container do bridge não mudou.

**Esperado:** FALHA antes da correção (provando que o teste de fato detecta o defeito), PASSA depois da correção.

**Observação também registrada no passo 3:** antes da correção esse comando constrói as duas imagens; depois da correção, constrói apenas a do `whatsapp-mcp`.

### Isolamento

O projeto de teste local é `wa-test`, cujo volume é `wa-test_whatsapp-store` — distinto do de produção. A limpeza remove apenas containers e o volume explicitamente nomeados do teste; nunca `down -v` e nunca um prune.

## Mudanças a aplicar

### `docker-compose.yml`

- Alterar SOMENTE o `image:` do serviço `whatsapp-mcp` para `whatsapp-mcp-server:latest`.
- Adicionar um comentário explicativo na linha `image:` do serviço `whatsapp-bridge`, alertando que ela permanece como `whatsapp-mcp:latest` de propósito e não deve ser "arrumada" — uma limpeza futura custaria uma recriação do bridge (risco de QR).
- Todos os demais campos permanecem byte-idênticos.

### `README.md`

Adicionar uma seção documentando:

- O comando de rebuild restrito a um serviço: `docker compose up -d --build --no-deps whatsapp-mcp`.
- Por que cada flag importa (`--build` para reconstruir a imagem alterada; `--no-deps` para impedir que o Compose inclua o `whatsapp-bridge` no escopo via `depends_on`).
- O risco de re-login por QR ao rodar `docker compose up -d --build` na stack completa, e que essa forma completa (usada no passo 3 de instalação do README) é apropriada apenas para a primeira instalação, não para atualizações de rotina.
