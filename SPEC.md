# Burnout 3 (Xbox) — Decompilação e Recompilação Nativa x86 (Windows/Linux)

## 1. Objetivo

Construir uma pipeline de ferramentas que parte de uma pasta já extraída do
disco original (filesystem XDVDFS do Xbox clássico) e produz, numa pasta de
saída separada, um executável **nativo x86** (Windows e/ou Linux) que roda o
jogo com paridade funcional ao original — reaproveitando o máximo possível de
bibliotecas/engine já identificadas, e reimplementando nativamente apenas o
que é proprietário do kernel do Xbox / XDK.

Não é emulação: o objetivo é decompilar o código para C legível, revisado
função por função, e recompilar isso num binário novo, ligado a uma camada de
compatibilidade (shim) que substitui as chamadas de kernel Xbox e D3D8 por
equivalentes nativos.

## 2. Fatos confirmados nesta investigação (ponto de partida)

- **Formato do disco**: XDVDFS (Xbox clássico), confirmado via assinatura
  `MICROSOFT*XBOX*MEDIA` e parsing completo do diretório raiz.
- **Executável principal**: `default.xbe` — 4.308.992 bytes, base
  `0x00010000`, build *retail* (não debug), 17 seções.
- **Seções do XBE**:
  - `.text` (2,86 MB) — código misto: jogo + RenderWare.
  - `.rdata`, `.data`, `.data1` — dados/globais.
  - `XMV`, `DSOUND`, `WMADEC`, `XONLINE`, `XNET`, `D3D`, `XGRPH`, `XPP`,
    `DOLBY`, `XON_RD` — bibliotecas estáticas do Microsoft XDK e codecs de
    terceiros, ~670 KB raw total. **Idênticas a milhares de outros jogos de
    Xbox** — candidatas a reconhecimento por assinatura, não RE do zero.
  - `$$XTIMAGE`, `$$XSIMAGE`, `.XTLID` — recursos (ícone/imagem de
    save/título), não código.
- **Engine**: RenderWare **3.6** ("`RW36Active`"), confirmada por 67 strings
  `$Id:` de Perforce com caminho de arquivo-fonte original (ex.
  `world/baworld.c`, `driver/xbox/xbrendst.c`,
  `pipe/p2/xbox/xbpixelshader.c`). Mesma geração de engine usada em GTA
  III/Vice City/San Andreas — comunidade de modding desses jogos já tem RE
  pública relevante.
- **Binário retail 100% stripped**: de 6.863 funções totais, só 65 (~0,9%)
  têm nome reconhecido automaticamente pelo banco de assinaturas genérico do
  Ghidra. Sem símbolos de debug reais.
- 742.363 instruções x86 desmontadas, 38.760 itens de dado definidos, 19
  blocos de memória mapeados.
- `dashupdate.xbe` e `update.xbe` (também no disco) são o atualizador de
  dashboard do Xbox — **não fazem parte do jogo, não precisam ser portados**.
- 129 strings contendo `D:\` ainda não extraídas/catalogadas — provável
  estrutura de build da Criterion/EA, pode revelar nomes de módulos internos.

## 3. Estratégia de "adivinhar e traduzir" funções (Cxbx-Reloaded)

Em vez de reverter cada uma das ~6.800 funções sem nome do zero, usamos duas
fontes externas já maduras:

1. **`XbSymbolDatabase`** (parte do projeto Cxbx-Reloaded) — banco de
   assinaturas (hash de bytes) por versão de XDK, cobrindo CRT, `D3D8LTCG`,
   `XGraphics`, `DirectSound`, `XOnline`, `XNet`, `XActEngine` etc. Resolve em
   massa a fração de código que é biblioteca padrão da Microsoft — exatamente
   as seções `D3D`, `DSOUND`, `XONLINE`, `XNET`, `XGRPH`, `XPP` que já
   isolamos. Não cobre RenderWare (é middleware de terceiro).
2. **Código-fonte do Cxbx-Reloaded** (kernel shim `CxbxKrnl` + wrapper D3D8
   `EmuD3D8`) como **referência de implementação** para a Fase 2 (camada de
   compatibilidade nativa) — não como emulador em runtime, e sim como mapa de
   "o que cada chamada de kernel/D3D8 precisa fazer", reimplementado
   estaticamente no nosso próprio shim.
   - ⚠️ Cxbx-Reloaded é GPL. Usar como *referência de leitura* é seguro;
     copiar código verbatim implica herdar a licença no nosso shim —
     decisão aberta, ver seção 6.

## 4. Arquitetura da pipeline

```
pasta do dump (XDVDFS extraído)
        │
        ▼
[1] extração/normalização ──────────► inventário de arquivos (Data/, Graphics/, sound/, tracks/, default.xbe)
        │
        ▼
[2] parse do XBE + geração de ELF sintético ──► projeto Ghidra persistente (análise completa)
        │
        ▼
[3] matching de bibliotecas conhecidas (XbSymbolDatabase + assinaturas RW) ──► função marcada: resolved_library | unresolved_game | thunk
        │
        ▼
[4] decompilação assistida (Ghidra) das funções "unresolved_game" ──► C revisado, por subsistema
        │
        ▼
[5] camada de runtime/compatibilidade (kernel_shim + d3d8_shim + audio/net shim)
        │
        ▼
[6] pipeline de assets (copiar/converter Data/Graphics/sound/tracks pro formato que o runtime recompilado espera)
        │
        ▼
[7] build nativo (CMake, Windows + Linux) ──► executável + assets na pasta de saída
        │
        ▼
[8] empacotamento final: script recebe (pasta do dump) + (build) → pasta de saída jogável
```

## 5. Estrutura de repositório

```
burnout3_decompiled/
  SPEC.md                      <- este documento
  tools/
    xbe/                       <- parsing XBE/XDVDFS (python) — extração e inventário
    ghidra/
      scripts/                 <- scripts Java p/ análise headless (já validados)
      run_analysis.sh          <- wrapper p/ rodar analyzeHeadless com env certo
    signatures/                <- bancos de assinatura (XbSymbolDatabase + RW), a integrar
  decomp/
    game/                      <- C reconstruído, específico do Burnout 3
    renderware/                <- stubs/matches da RenderWare 3.6 identificados
  runtime/
    kernel_shim/               <- reimplementação nativa das chamadas de kernel Xbox usadas
    d3d8_shim/                 <- tradução D3D8 -> backend moderno
    audio_net_shim/            <- DirectSound/WMA/XOnline/XNet -> backend nativo
  assets_pipeline/             <- scripts de conversão/empacotamento de assets
  build/                       <- saída de build (gerada, não versionada)
  docs/
    findings/                  <- notas de RE: lista de funções RW identificadas, strings D:\, etc.
```

## 6. Fases e entregáveis

### Fase 0 — Infraestrutura de análise (em andamento)
- [x] Confirmar formato do disco e localizar `default.xbe`.
- [x] Parser de header XBE funcional.
- [x] Pipeline Ghidra headless funcionando (contagem de funções, busca de strings).
- [ ] Script único `extract_and_analyze.sh`: dump → ELF sintético → análise
      completa → projeto Ghidra persistente (reaproveitável entre consultas).
- [ ] Catalogar as 129 strings `D:\` e demais strings de debug.

### Fase 1 — Catalogação de bibliotecas conhecidas
- [x] Integrar `XbSymbolDatabase` (Cxbx-Reloaded, MIT) e rodar matching
      contra o `default.xbe` — ver `tools/signatures/README.md` e
      `docs/findings/xbsymboldatabase_matches_default_xbe.{txt,json}`.
      Resultado: **362 endereços resolvidos** (320 funções + 98
      variáveis/constantes) de 6.863 funções totais (~5%), cobrindo
      DSOUND/D3D8LTCG/XAPILIB/D3D8/XONLINES. `XNET`, `XGRPH`, `XPP`,
      `DOLBY`, `WMADEC`, `XMV` não bateram com esta versão do banco —
      precisam de assinaturas próprias ou outra versão do XDK catalogada.
- [x] Aplicar os matches no projeto Ghidra persistente
      (`tools/ghidra/scripts/ApplyLibraryMatches.java`): **251 funções
      renomeadas + 36 variáveis rotuladas** (6 entradas eram offsets de
      struct, não endereços — puladas de propósito; 69 "FUN" não bateram
      com um limite de função existente no Ghidra, provavelmente código
      inline/sem prólogo padrão). Total de funções com nome não-genérico
      saltou de 65 → **316 de 6.863 (~4,6%)**.
- [x] Investigado por que XNET/XGRPH/XPP/DOLBY/WMADEC/XMV não bateram — **não
      é descompasso de versão** (todas as 11 bibliotecas declaradas no XBE
      usam o mesmo build XDK 5849). É lacuna real do banco público:
      - `XGRAPHCL` (variante LTCG do XGraphic, a que o Burnout 3 realmente
        usa) tem uma entrada *comentada/vazia* no código-fonte do
        `XbSymbolDatabase` — só a variante não-LTCG (`XGRAPHC`) tem dados.
      - `XVOICE` e `XMV` não têm nenhuma entrada no banco.
      - `LIBC`/`LIBCMT`/`LIBCPMT` (CRT) estão fora do escopo da ferramenta
        por design.
      - `XNET` nem é uma dependência declarada separada no XBE — vem
        embutido em `XONLINES`.
      - `DOLBY`/`WMADEC` são seções do XBE mas não aparecem na tabela de
        bibliotecas declaradas — prováveis módulos de codec internos ao
        XACTENG/DSOUND, não dependências próprias.
      Ou seja: escrever assinaturas OOVPA para `XGRAPHCL`/`XVOICE`/`XMV` é
      um trabalho real e autocontido (e dá pra contribuir de volta pro
      Cxbx-Reloaded), não uma questão de reconfiguração.
- [x] RenderWare 3.6: sem banco de assinaturas pronto pra Xbox. Usamos
      [ifarbod/renderware3-flirt](https://github.com/ifarbod/renderware3-flirt)
      (FLIRT da RW 3.6.0.3 compilada p/ GTA SA PC, D3D9, outro
      compilador/plataforma) via `tools/signatures/flirt_match.py` — matcher
      FLIRT standalone em Python puro (porque o script Jython original só
      roda no Ghidra <12). Resultado: **13 funções confiáveis** aplicadas
      no Ghidra, de 33 matches brutos (resto era ruído: D3DX/libpng/CRT de
      uma ferramenta de conversão offline, e colisões em funções curtas).
      Yield baixo era esperado — FLIRT exige quase paridade byte-a-byte, e
      aqui plataforma/compilador diferem. Ver `tools/signatures/README.md`.
- [ ] As ~90 unidades de compilação da RenderWare identificadas pelos
      `$Id:` do Perforce (seção 2) vão precisar de decompilação manual —
      não há mais atalho de assinatura conhecido pra explorar aqui.
- [ ] Saída: tabela (JSON/markdown) com status por função
      (`resolved_library` / `unresolved_game` / `thunk`).

### Fase 2 — Camada de runtime/compatibilidade
- [ ] Mapear a kernel thunk table do XBE: quais das ~380 chamadas de kernel
      o binário realmente usa.
- [ ] Implementar `kernel_shim` nativo (threads, memória, I/O, timers),
      usando o `CxbxKrnl` do Cxbx-Reloaded como referência de leitura.
- [ ] Implementar `d3d8_shim` (tradução das chamadas D3D8 realmente usadas
      pelo driver Xbox da RenderWare para backend moderno), usando `EmuD3D8`
      como referência.
- [ ] Implementar shim de áudio/rede equivalente.

### Fase 3 — Decompilação função a função (o grosso do trabalho)
- [ ] Priorizar pelas funções `unresolved_game` da Fase 1.
- [ ] Fluxo por função: pseudocódigo Ghidra → C revisado → teste de paridade
      → commit.
- [ ] Rastreamento de progresso por subsistema (física, IA de trânsito,
      takedown, menu, HUD, networking).

### Fase 4 — Pipeline de assets
- [ ] Decidir: manter assets no formato RenderWare original (menor risco,
      funciona com o loader da própria engine recompilada) ou converter para
      formato próprio (acréscimo opcional, não obrigatório na v1).
- [ ] Script que leva `Data/`, `Graphics/`, `sound/`, `tracks/` do dump pra
      pasta de saída no formato esperado pelo runtime.

### Fase 5 — Build e empacotamento final
- [ ] Build system CMake multiplataforma (Windows MSVC/MinGW, Linux gcc/clang).
- [ ] Script final `build_release.sh`/`.ps1`: (pasta do dump) + (repo
      compilado) → pasta de saída pronta pra rodar.
- [ ] Teste de fumaça: sobe, carrega menu, entra numa pista, física básica
      funciona.

### Fase 6 — Validação e paridade
- [ ] Comparar comportamento com o original usando Cxbx-Reloaded como
      *referência visual/comportamental* lado a lado (não como base do port).
- [ ] Lista de regressões conhecidas / features ainda não portadas.

## 7. Decisões abertas (precisam de você)

1. Plataforma prioritária: Windows primeiro, Linux primeiro, ou os dois em
   paralelo desde o início?
2. Backend gráfico do shim: D3D11/Vulkan nativo, ou lib de abstração
   (bgfx/SDL_gpu) pra cobrir Windows+Linux com menos código duplicado?
3. Assets: manter formato RenderWare original ou converter desde já?
4. Linguagem/toolchain: C puro (mais fiel ao original) ou permitir C++
   moderno nas partes novas (shim, build system)?
5. Uso do Cxbx-Reloaded: só como referência de leitura (sem risco de
   licença), ou reaproveitar trechos de código diretamente (aí o shim
   resultante herda GPL)?
   **Resolvido parcialmente**: `XbSymbolDatabase` (usado na Fase 1) é MIT,
   sem entanglement de licença. A dúvida de GPL só se aplica se, na Fase 2,
   formos copiar trechos do `CxbxKrnl`/`EmuD3D8` (esses sim são GPL, parte
   do Cxbx-Reloaded principal).
6. Escopo de distribuição: **decidido** — repositório fica aberto no git do
   usuário. GPL não é um impeditivo em si (é compatível com distribuição
   aberta); o ponto de atenção real continua sendo o código/assets
   proprietários da Criterion/EA (RenderWare + lógica do jogo), que são
   direitos autorais de terceiros independentemente da licença das
   ferramentas de RE usadas.

## 8. Riscos conhecidos

- RenderWare 3.6 e o código do jogo continuam sendo propriedade da
  Criterion/EA — reconhecer arquivos por nome não muda a situação de direitos
  autorais do restante do binário.
- ~6.800 funções sem símbolo no total; mesmo com bom aproveitamento de
  assinatura de biblioteca (Fase 1), a fração específica do jogo ainda
  representa um volume de trabalho humano significativo (estimativa: centenas
  a ~1000+ horas, a depender de quanto a Fase 1 reduzir).
- A thunk table de kernel é fixa: qualquer chamada mapeada errado quebra o
  jogo silenciosamente ou crasha. A Fase 2 precisa de testes sólidos antes de
  avançar a Fase 3 em paralelo.

## 9. Addendum — pivô pra recompilação estática (xboxrecomp)

As fases acima descrevem o plano original de decompilação manual. Esse
plano foi pausado em favor de usar o **`sp00nznet/xboxrecomp`** (toolkit de
recompilação estática já maduro, com um fork dedicado ao Burnout3) — ver
decisão e detalhes na conversa, não repetidos aqui pra não duplicar.

Resumo do estado prático:
- Pipeline completo (`xbe_parser → disasm → func_id → abi_analysis →
  recomp`) rodado com sucesso contra nosso `default.xbe`: 24.494 funções,
  cross-compilado pra Windows via MinGW a partir do Linux.
- Debugging de boot em andamento — ver
  [`docs/findings/static_recomp_boot_debug.md`](findings/static_recomp_boot_debug.md)
  pro estado exato (fixes já aplicados, trava atual identificada em
  `sub_00351B10`, ferramentas de diagnóstico reaproveitáveis).
- Repositórios de terceiros (`vendor/xboxrecomp`, `vendor/burnout3`)
  clonados mas não versionados (ver `.gitignore`) — fonte de verdade é o
  upstream do GitHub, nosso repo só guarda os fixes/configuração locais
  (`config/seed_functions.json`, patches em `main.c`) via o findings doc
  acima até decidirmos se vale manter um fork próprio.
