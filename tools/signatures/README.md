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
`XGRAPHCL`/`XVOICE`/`XMV` (contribuível de volta ao Cxbx-Reloaded), ou já
seguir pra RenderWare — que é a fatia maior e não tem ferramenta pronta.
