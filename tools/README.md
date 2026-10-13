# tools/

Ferramentas de análise validadas durante a investigação inicial (ver SPEC.md).

- `xbe/xbe_to_elf.py` — parseia um `default.xbe` do Xbox clássico e
  (opcionalmente) gera um ELF32/x86 sintético, mapeando cada seção do XBE
  para um `PT_LOAD` com a VA correta. Usado para abrir o executável em
  ferramentas que não falam XBE nativamente (Ghidra, etc.).

  ```
  python3 tools/xbe/xbe_to_elf.py <dump>/default.xbe \
      --out-elf build/default.synthetic.elf \
      --json build/default.xbe.json
  ```

- `xbe/xbe_library_versions.py` — lista as bibliotecas estáticas declaradas
  no XBE (nome + versão/build do XDK). Usado pra confirmar se um gap de
  matching é descompasso de versão ou lacuna real do banco de assinaturas.
- `ghidra/scripts/CountFunctions.java` — métricas de escala (nº de funções,
  quantas têm nome não-genérico, instruções desmontadas, etc.).
- `ghidra/scripts/ProbeStrings.java` — varre strings definidas procurando
  padrões conhecidos (RenderWare, caminhos de debug, etc.) — foi assim que
  identificamos a RenderWare 3.6 pelos `$Id:` do Perforce.
- `ghidra/scripts/ApplyLibraryMatches.java` — lê o `.tsv` gerado por
  `signatures/parse_matches.py` e renomeia funções / rotula variáveis no
  projeto Ghidra persistente (ver `signatures/README.md`).
- `ghidra/run_analysis.sh` — wrapper do `analyzeHeadless`. Exige
  `GHIDRA_INSTALL_DIR` apontando para um Ghidra 12.1.x extraído e um JDK 21+
  no PATH.

  ```
  # análise completa (uma vez, ~9-10 min pro default.xbe atual)
  GHIDRA_INSTALL_DIR=~/.local/share/ghidra/ghidra_12.1.4_PUBLIC \
    tools/ghidra/run_analysis.sh import build/default.synthetic.elf build/ghidra_proj CountFunctions.java

  # consultas seguintes reaproveitando a análise salva (segundos)
  GHIDRA_INSTALL_DIR=~/.local/share/ghidra/ghidra_12.1.4_PUBLIC \
    tools/ghidra/run_analysis.sh process default.synthetic.elf build/ghidra_proj ProbeStrings.java
  ```

- `signatures/` — Fase 1: `setup.sh` clona+compila o `XbSymbolDatabase` do
  Cxbx-Reloaded (MIT), `parse_matches.py` estrutura a saída. Ver
  `signatures/README.md` pros resultados no `default.xbe` e as lacunas
  conhecidas do banco (XGRAPHCL/XVOICE/XMV). Assinaturas de RenderWare ainda
  não começadas.
