# tools/signatures/

Fase 1 da SPEC.md: identificar, antes de decompilar à mão, quais das ~6.863
funções do `default.xbe` já são bibliotecas conhecidas do XDK (não código do
jogo).

## XbSymbolDatabase (Cxbx-Reloaded)

MIT licensed. Usa o formato OOVPA (Ordered Offset-Value Pair Array, criado
pelo Caustik pro projeto Cxbx original) pra reconhecer funções/variáveis de
biblioteca por assinatura de bytes, sem precisar de símbolos de debug.

```
./setup.sh   # clona + compila (não versionado, ver .gitignore)

XbSymbolDatabase/build/projects/cli/XbSymbolDatabaseCLI <dump>/default.xbe -e \
    > ../../docs/findings/xbsymboldatabase_matches_default_xbe.txt

python3 parse_matches.py ../../docs/findings/xbsymboldatabase_matches_default_xbe.txt \
    --json ../../docs/findings/xbsymboldatabase_matches_default_xbe.json
```

⚠️ Não misture stdout com stderr (`2>&1`) ao capturar a saída — os streams
intercalam e corrompem linhas no meio. Rode sem `-v`, ou capture em arquivos
separados.

## Resultado no `default.xbe` do Burnout 3 (PAL)

362 endereços únicos resolvidos (320 funções + 98 variáveis/constantes, com
algumas duplicatas de nome no mesmo endereço) de 6.863 funções totais
reportadas pelo Ghidra — ou seja, cobre diretamente ~5% das funções do
binário.

| Biblioteca | Matches |
|---|---|
| DSOUND    | 149 |
| D3D8LTCG  | 117 |
| XAPILIB   | 58  |
| D3D8      | 25 (majoritariamente constantes `D3DRS_*`) |
| XONLINES  | 13 |

### Por que XNET/XGRPH/XPP/DOLBY/WMADEC/XMV não bateram

Investigado via `tools/xbe/xbe_library_versions.py` + leitura do código-fonte
do `libXbSymbolDatabase.c`. **Não é descompasso de versão** — as 11
bibliotecas que o `default.xbe` declara (`XAPILIB`, `XMV`, `DSOUND`,
`XBOXKRNL`, `XONLINES`, `XVOICE`, `LIBCMT`, `LIBCPMT`, `LIBC`, `D3D8LTCG`,
`XGRAPHCL`) são todas do **mesmo build XDK 5849**. São lacunas reais e
específicas:

- `XGRAPHCL` (a variante LTCG do XGraphic — a que o Burnout 3 usa) tem uma
  entrada **comentada/vazia** no `SymbolDBList` do `XbSymbolDatabase`; só a
  variante não-LTCG (`XGRAPHC`) tem assinaturas de fato.
- `XVOICE` e `XMV` não têm nenhuma entrada no banco — zero cobertura.
- `LIBC`/`LIBCMT`/`LIBCPMT` (CRT) estão fora do escopo da ferramenta por
  design (reconhecimento de CRT é trabalho de outra ferramenta, ex. o
  próprio FunctionID do Ghidra).
- `XNET` nem é dependência declarada separadamente no XBE — vem embutido em
  `XONLINES`.
- `DOLBY`/`WMADEC` são seções do binário mas não aparecem na tabela de
  bibliotecas declaradas — prováveis módulos de codec internos ao
  XACTENG/DSOUND.

RenderWare (motor do jogo) nunca apareceria aqui de qualquer forma — é
middleware de terceiro, fora do XDK.

## Aplicação no Ghidra

`tools/ghidra/scripts/ApplyLibraryMatches.java` lê o `.tsv` (gerado com
`parse_matches.py --tsv`) e aplica no projeto persistente:

```
tools/ghidra/run_analysis.sh process default.synthetic.elf <project_dir> \
    "ApplyLibraryMatches.java docs/findings/xbsymboldatabase_matches_default_xbe.tsv"
```

Resultado na rodada atual: **251 funções renomeadas + 36 variáveis
rotuladas** (6 entradas eram offsets de struct — puladas de propósito; 69
"FUN" caíram em endereços sem limite de função reconhecido pelo Ghidra,
provavelmente código inline). Total de funções com nome não-genérico:
65 → **316 de 6.863 (~4,6%)**.

**Próximo passo natural**: escrever assinaturas OOVPA próprias para
`XGRAPHCL`/`XVOICE`/`XMV` (contribuível de volta ao Cxbx-Reloaded) — ainda
não feito, ver seção abaixo pra RenderWare.

## RenderWare 3.6 — FLIRT cross-platform matching

Não existe banco de assinaturas pronto pra RenderWare no Xbox. O mais
próximo publicamente disponível é
[ifarbod/renderware3-flirt](https://github.com/ifarbod/renderware3-flirt):
599 arquivos `.sig` (formato IDA FLIRT) gerados a partir do **RenderWare
3.6.0.3, compilado com VC7 para x86 — mas pra GTA San Andreas PC, build
D3D9 Release**, não pra Xbox/D3D8. Mesma versão major.minor da engine
(3.6) e mesma arquitetura (x86), mas plataforma/driver gráfico e compilador
diferentes.

```
git clone https://github.com/ifarbod/renderware3-flirt.git   # não versionado (ver .gitignore)

python3 flirt_match.py <dump>/default.xbe \
    ../../docs/findings/ghidra_function_addresses.tsv \
    renderware3-flirt/rw3603_vc7_x86 \
    --out ../../docs/findings/renderware_flirt_matches.tsv
```

`flirt_match.py` é uma reimplementação standalone (puro Python 3, sem
Ghidra) do parser/matcher FLIRT, portada de
[NWMonster/ApplySig](https://github.com/NWMonster/ApplySig) — que só roda
no Jython antigo do Ghidra, removido na versão 12. Em vez de depender do
Ghidra em tempo real, ela lê os endereços de função já exportados
(`ExportFunctionAddresses.java`) e testa cada um contra cada `.sig`.

**Resultado no `default.xbe`**: 33 matches brutos em 599 arquivos de
assinatura, mas a maioria é ruído — o conjunto de `.sig` também carrega
bibliotecas de terceiro que nada têm a ver com o motor (D3DX, libpng, CRT
genérica como `logf`), prováveis de uma ferramenta de conversão de textura
offline do SDK PC, e colisões em funções curtas (`RpLODAtomicSetCamera`
batendo em 4 endereços diferentes). Filtrando por convenção de nomes real
da RenderWare (`Rw`/`Rp`/`Rt`, prefixo C) e descartando ambiguidades e
qualquer coisa `D3D9` (Xbox só tem D3D8): **13 matches confiáveis**,
salvos em `renderware_flirt_matches.confident.tsv` e já aplicados no
Ghidra:

| Endereço | Nome |
|---|---|
| 0x001b3990 | `RpSkinGetVertexBoneWeights` |
| 0x001d9640 | `RwCameraEndUpdate` |
| 0x001d9650 | `RwCameraBeginUpdate` |
| 0x001dc110 | `rpLtMapGenericPipelinesDestroy` |
| 0x001dd640 | `RwFrameGetLTM` |
| 0x001e0a00 | `RwMatrixUpdate` |
| 0x001e1140 | `RwFreeListPurgeAllFreeLists` |
| 0x001e8620 | `rwpathisabsolute` |
| 0x001e8ed0 | `rwIm3DCreatePlatformTransformPipeline` |
| 0x001fcd80 | `RtFileSystemIsDefaultedToAsync` |
| 0x00211620 | `rt2dMaestroGetAnimNodeByIndex` |
| 0x0021c5e0 | `RpSkinGetVertexBoneIndices` |
| 0x0021c7a0 | `rpSkinGetMeshBoneRLECount` |

**Conclusão**: matching cross-plataforma/cross-compilador rende pouco
(13 de ~6.500 funções ainda sem nome) porque FLIRT exige correspondência
quase byte-a-byte — build diferente, otimizações diferentes, já quebra a
maioria dos matches. As ~90 unidades de compilação da RenderWare que já
identificamos pelos `$Id:` (ver SPEC.md) vão precisar de decompilação
manual mesmo; esses 13 nomes servem de ponto de partida/âncora, não de
solução.
