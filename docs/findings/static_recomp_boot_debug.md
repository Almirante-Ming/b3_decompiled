# Debug da recompilação estática — boot até tela preta

Contexto: `vendor/burnout3` + `vendor/xboxrecomp` (clonados, não versionados aqui —
ver `.gitignore`), cross-compilados com MinGW-w64 a partir do nosso
`default.xbe` real. Build e testes rodados numa máquina Windows separada
(exe compilado aqui no Linux via MinGW, copiado pra lá).

## Linha do tempo dos fixes aplicados

1. **Gap de função no dispatcher ICALL (`0x00355240`)** — causava
   `[ICALL] Failed to resolve VA 0x00355240` repetido, zero desenhos.
   Causa: 386 gaps entre funções detectadas por `tools.disasm` em todo o
   binário (354 no `.text`, 10 no `D3D`) — funções reais só alcançadas via
   vtable/dispatch dinâmico, nunca chamadas estaticamente, então o
   detector de função nunca as achou sozinho.
   Fix: gerei seeds pra todos os 386 gaps (`config/seed_functions.json`,
   12 originais → 398 total) e re-rodei `disasm → func_id → abi_analysis →
   recomp`. 24.120 → 24.494 funções. **Resolvido** — esse ICALL específico
   não aparece mais.
2. **`RECOMP_PB_EXEC`** — investigado e descartado como causa. Já vem
   ligado por padrão em `main.c` (`if (!getenv(...)) _putenv_s(...,"1")`).
   Setar de novo não tem efeito.
3. **Resolução de símbolo do `RECOMP_HANG_WATCHDOG` não funcionava** — o
   código assume um build MSVC com PDB (`SymFromAddr` via dbghelp); nosso
   build é GCC/MinGW, sem PDB, então nenhum símbolo resolvia (nem o
   endereço aparecia, só ficava em branco).
   Fix: patch em `print_host_symbol()` (`src/game/main.c`) pra imprimir o
   endereço bruto (`at host 0x...`) quando `SymFromAddr` falha, e print do
   module base (`GetModuleHandle(NULL)`) no início do `main()` — o exe
   retém a tabela de símbolos completa (`nm bin/burnout3.exe`, 26.560
   símbolos, não foi stripado), então dá pra mapear endereço de runtime →
   função offline: `link_addr = host_addr - module_base_runtime +
   0x140000000`.

## Estado atual: trava em loop de espera ocupada após 1 burst de GPU

Depois do fix do ICALL, o boot avança bem mais (passa por mais chamadas de
kernel, processa `KeConnectInterrupt vector 3 -> routine 0x00356E70`, faz
**um único burst** de comandos de GPU — clear + setup de surface — e então:

- `DMA_PUT` para de avançar pra sempre (`0x01E1F054` repetido
  indefinidamente)
- `indirect calls` para de crescer (`+16295` na primeira amostra, `+0` em
  todas as seguintes)
- Uma thread (a thread principal do jogo, criada via
  `PsCreateSystemThreadEx`, guest VA `0x001D1BD8`) fica em ~100% de CPU,
  sem nunca fazer nova chamada indireta.

### Cadeia de chamada exata (resolvida via `nm` + delta de ASLR)

```
sub_001D1BD8  (rotina da thread — rw_driver_xbox)
  -> sub_001D2B53             (rw_driver_xbox)
    -> sub_00156740           (game_engine, rw_consumer)
      -> sub_00015B20         (game_engine, rw_consumer)
        -> sub_0003CAB0       (game_engine, rw_consumer)
          -> sub_0003D8A0     (game_engine, rw_consumer)
            -> sub_00351430   (rw_driver_xbox, seção D3D)
              -> sub_003513F0 (rw_driver_xbox)
                -> sub_00351260 (rw_driver_xbox)
                  -> sub_00351B10   <-- PRESO AQUI, 358 bytes, 118 instruções
```

`sub_00351B10` fica na seção `D3D` (0x0034C680–0x00360E00), classificada
pelo `func_id` como `rw_driver_xbox` — ou seja, é o driver gráfico
genérico da RenderWare pra Xbox (não código específico do Burnout3).

Em paralelo, achei outra thread (`nv2a_ack_thread`) que é **nossa própria**
thread de runtime (não gerada do jogo) tentando simular reconhecimento de
operações de hardware da GPU via `SleepEx` periódico.

### Hipótese mais provável

`sub_00351B10` é um loop de espera ocupada de baixo nível do driver
RenderWare esperando um bit de status de hardware da GPU mudar (ex:
"GPU pronta"/vblank), e nossa emulação do NV2A (`nv2a_core.c`/
`nv2a_mmio_hook.c`) não fornece esse bit da forma que esse loop espera —
mesmo com o `nv2a_ack_thread` rodando em paralelo tentando simular algum
tipo de acknowledgment.

### Ferramentas de diagnóstico que valem a pena guardar

- `nm bin/burnout3.exe | awk '$2=="T" || $2=="t"{print $1,$3}' | sort` —
  tabela de símbolos completa (não stripado).
- Delta de ASLR: `link_addr = host_runtime_addr - module_base_runtime +
  0x140000000` (módulo base impresso no boot via `[BASE] module base =
  ...`, já adicionado em `main()`).
- `RECOMP_HANG_WATCHDOG=<segundos>` — amostra todas as threads
  periodicamente, mostra pilha de chamada nativa + contador de chamadas
  indiretas (se não crescer entre amostras, é trava de verdade, não só
  lento).
- `build/xr/disasm/functions.json` e `build/xr/func_id/identified_functions.json`
  — já têm a classificação (`rw_driver_xbox`, `game_engine`, etc.) e grafo
  de chamadas (`calls_to`/`called_by`) de cada função, úteis pra navegar
  sem precisar abrir um decompilador.

## Causa raiz encontrada e corrigida

Instrumentei `fence_mirrors_tick()` com `RECOMP_FENCE_TRACE=1` (print
uma vez/segundo: dev/get_ptr/fence/put, e qual `continue` disparou se
algum). Resultado:

```
[FENCE] #0 dev=0x07060504 get_off=0x30 put_off=0x2C -- dev+get_off or dev+put_off unreadable
```

`dev=0x07060504` é lixo de memória não-inicializada — o endereço onde
`src/game/main.c` registra o fence mirror (`xbox_Nv2aMirrorFence`) nunca
continha um ponteiro válido pro device D3D.

Causa: **`main.c` (fork do Burnout3 no `xboxrecomp`) usa o endereço
`0x0035FB48`, calibrado contra o build NTSC-US do jogo** — mas nosso
`default.xbe` é o release **PAL "Europe (En,Es,Nl,Sv)"**, cujo linker
colocou o mesmo global (`D3D_g_pDevice`) em **`0x0035FEE8`** (valor que já
tínhamos confirmado duas vezes: via XbSymbolDatabase na Fase 1, e via
desmontagem direta de `sub_00351B10`, que lê exatamente
`mov esi, [0x35FEE8]`). A própria rotina de reserva de pushbuffer também
está em endereço diferente: `sub_00351B10` aqui vs. `sub_00351770` no
build deles — mesma lib `D3D8LTCG` (mesmo XDK 5849), só desloca por
diferença de linkagem entre regiões.

**Fix aplicado**: troquei as duas chamadas
(`xbox_Nv2aMirrorFence`/`xbox_Nv2aFrameCounter`) em `main.c` de
`0x0035FB48` pra `0x0035FEE8`. Offsets internos (`+0x2C`, `+0x30`,
`+0x1DE8`) continuam os mesmos — são da biblioteca `D3D8LTCG` em si, que
é idêntica bit-a-bit entre regiões/títulos na mesma versão de XDK; só o
endereço do ponteiro-pro-device muda por build.

## Depois do fix do fence: boot avança até carregamento de assets

O fix do ponteiro PAL destravou o boot de verdade. O log seguinte mostra
fence atualizando, `DMA_PUT` avançando, APU iniciando, assets streamando
do disco e **41.568 chamadas indiretas** (antes: parava em ~16k e nunca
crescia). O jogo agora morre mais tarde, durante o carregamento de assets,
com dois sintomas novos:

### 1. Decodificador de MMIO incompleto (corrigido)

```
[OHCI0] undecoded access at +0x00C: 44 23 04 11 8B 88
```

`44 23 04 11` = `and r8d, [rcx+rdx*1]` — REX.R=1, opcode `0x23`
(`AND r32, r/m32`), ModRM `0x04` (mod=00, rm=100 → SIB), SIB `0x11`
(escala ×1, index=rdx, base=rcx). Offset `+0x00C` é
`HcInterruptStatus`, exatamente o registrador que um driver OHCI testa
em loop.

Causa: `vendor/xboxrecomp/src/platform/mmio_decode.h` (decodificador
compartilhado, chamado por `xbox_OhciHandleMmio` em `src/usb/ohci.c:1351`)
só cobria a **direção de escrita** das ALU (`08/09` OR, `20/21` AND) e
nenhuma forma com imediato. Qualquer `op r, [mem]` caía no `return 0`.

Fix aplicado — preenchi a classe inteira de uma vez em vez de opcode por
opcode (cada ciclo custa rebuild + cópia + teste na outra máquina):

- `00..03, 08..0B, 20..23, 28..2B, 30..33, 38..3B` — as 6 ALU ops
  (ADD/OR/AND/SUB/XOR/CMP) nas duas direções e nas duas larguras, num
  único `case` unificado (bit 1 do opcode = direção, bit 0 = largura,
  `(op>>3)&7` = qual operação).
- `80/81/83` — grupo 1 com imediato (`83` com sinal estendido, senão
  `and dword [r], -2` limparia byte inteiro em vez de um bit).
- `F6/F7 /0` — `TEST r/m, imm`.
- `C7` com prefixo `66` lia imediato de 4 bytes em vez de 2 (bug
  pré-existente; `Rip` avançava errado).
- Helpers novos: `mmio_set_reg()` (escrita de 32 bits zera a metade alta,
  como o hardware) e `mmio_alu()`.

**ADC/SBB ficaram de fora de propósito** — dependem do CF de entrada, que
nada aqui rastreia, e carry errado é valor errado silencioso. Retornam 0,
respeitando a regra do próprio arquivo: falhar nomeando o opcode é muito
melhor que passar por cima de instrução não entendida.

Validado com `x86_64-w64-mingw32-gcc -c -O2 -Wall -Wextra` (header
isolado, zero warnings).

### 2. `0xFED1000C` não pertence a nenhum controller (em aberto)

```
[FAULT] read from host 0x00000000FED1000C
  guest regs: ecx=FED00000 edx=00000000 eax=80000073 esi=80000418
```

`hc_for()` (`src/usb/ohci.c:1326`) itera **só 2** controllers,
`XBOX_OHCI0_BASE 0xFED00000` e `XBOX_OHCI1_BASE 0xFED08000`, com
`XBOX_OHCI_SIZE 0x1000` cada — então `0xFED1000C` está fora dos dois,
`xbox_OhciOwnsAddress()` retorna 0, ninguém reivindica a página.

Detalhe importante do diagnóstico: o relatório em `veh_handler`
(`src/game/main.c:244`) só imprime `= guest 0x...` quando o endereço está
**acima** de `g_xbox_mem_offset`. Não imprimiu, logo `0xFED1000C` foi
dereferenciado **cru**, sem a tradução guest→host.

Hipótese (a testar com o build novo, antes de patchar): é **cascata** do
item 1, não bug independente. Quando `mmio_emulate` retornava 0, o VEH
caía pra `EXCEPTION_CONTINUE_SEARCH`, o SEH do guest (modelado — ver
`g_seh_ebp`) pegava a exceção e seguiu com registrador lixo, produzindo o
ponteiro inválido depois. Se for isso, o fix do decodificador resolve os
dois e não há nada a fazer aqui. Se persistir, aí sim decidir entre
estender a aperture ou modelar um terceiro controller.

### 3. Seed de ICALL adicionado

`[ICALL] Failed to resolve VA 0x00363104` (seção XPP) → adicionado em
`config/seed_functions.json`, **399 → 400 seeds**.

## O jogo roda: boot resolvido, sobra só rasterização

Com o decodificador completo + seed novo, o executável **roda de verdade**:
contador de FPS na tela, ~80–95 fps, 15.000 draws, assets carregando,
`[FBWIN] framebuffer window open (640x480)`, chamadas indiretas subindo
continuamente (86.567 e crescendo — antes empacava em 41k).

Confirmado de passagem: **os itens 1 e 2 morreram juntos**. Nenhum
`[OHCI0] undecoded access` e nenhum `[FAULT] 0xFED1000C` no log novo. A
hipótese de cascata estava certa: o `0xFED1000C` era consequência do
decodificador incompleto, não bug independente. Não há terceiro controller
a modelar.

### O que falta: 100% da geometria descartada como "atrás do olho"

```
[GPU] vertex programs: 15012 batches, 3587382 vertices;
                       1794096 triangles dropped behind the eye
[GPU]   of the rest: 0 degenerate/NaN, 0 off-surface, 0 drawn; pixels 0 written
[GPU]   x 0..0 y 0..0 z 0..0
[GPU] brightest pixel written 0x00000000
```

`1794096 × 2 = 3588192` ≈ os 3.587.382 vértices: **nada sobra**. O contador
`tris_behind` (`nv2a_pb_exec.c:2133`) só incrementa quando os três vértices
têm `pos[3] <= NV_CLIP_W` (1e-3), ou seja o programa de vértice emite `w≈0`
em todo vértice.

Descartado: **não é default de atributo**. `fetch_attr()`
(`nv2a_pb_exec.c:466`) já inicializa `out[3] = 1.0f`, então um atributo
float de 2 componentes (`v0:2/2`, que casa com as coordenadas de tela
`x 98.6..268.1 y 151.9..380.0`) entra corretamente como `(x, y, 0, 1)`.

Hipótese atual: as **constantes do programa de vértice estão zeradas** —
a matriz de transformação e/ou o `scale`/`offset` de viewport que o código
lê via `nv2a_vsh_constant(58)`/`(59)`. Com constantes zero a transformação
dá `(0,0,0,0)`, o que explica `w=0` universal e os `x 0..0 y 0..0`. Isso
não aparecia antes porque o caminho 2D calibrado (INLINE_ARRAY,
`nv2a_pgraph_d3d11.c`) não usa programa; agora são 15.012 batches **todos**
via programa, caindo no caminho `xf`.

Reforça a hipótese o histograma de métodos **não-tratadas** (467.968 no
total, 159 distintas), decodificando os valores do topo:

| método | último valor | o que é |
|---|---|---|
| `0x02C0` | `0x02800000` | 640 ≪ 16 — viewport/clip em 16.16 |
| `0x02E0` | `0x01E00000` | 480 ≪ 16 — idem |
| `0x0394`/`0x0398` | `0x4B7FFFFF` | 16777215.0 — `SET_CLIP_MIN`/`MAX` (profundidade 24 bits) |
| `0x1D70` | `0x0000EB47` | casa com o valor do fence — release do semáforo |

### Próximo passo: `RECOMP_VSH_TRACE`

Já existe diagnóstico pronto (`nv2a_pb_exec.c:2197`): imprime setup de
atributos, entradas cruas, as constantes `c[58] c[59] c[96] c[97]
c[112..115]` e as posições de saída. Isso decide entre "constantes
zeradas" e outra causa, sem precisar adivinhar.

**Mas o gatilho estava errado para runs curtos**: só disparava em
`batches_program % 20000 == 0`, e a execução chegou a 15.012 batches — não
imprimiria nada. Pior, trace que não imprime nada é indistinguível de
trace que rodou e não achou problema. Mudei para disparar **no primeiro
batch** também, mantendo o intervalo de 20000 para pegar a cena 3D depois
do menu.

## Causa raiz da tela preta: `oPos.w` sem default

O `RECOMP_VSH_TRACE` **falsificou a hipótese das constantes** e entregou a
causa real:

```
v0 = 268.091  151.947  0  1
c[58] = 320  -224  1.67772e+07  0     <- viewport scale (640/2, -448/2, 24-bit)
c[59] = 320.531  240.531  0  0        <- viewport offset
out0 pos = 268.091  167.947  0  0
```

As constantes estão **corretas**, não zeradas. E a saída está certa também:
`x` passa intacto (`268.091` → `268.091`) e `y` ganha exatamente `+16`, que
é o `+0+16` do `clip 640x448+0+16`. É um **passthrough de geometria já em
espaço de tela** (a UI 2D), funcionando — nem usa o viewport transform.

O único componente errado é `w`: entra `1` (via `fetch_attr`), sai `0`.

**Causa**: `nv2a_vsh_run()` (`nv2a_vsh_interp.c:339`) zerava `opos` inteiro.
O arquivo já aplicava o default `(0,0,0,1)` para cores (`outregs[3][3]`,
`[4][3]`) e para texcoords (`outregs[9..12][3]`), citando o xemu e o
raciocínio de que `q = 0` jogaria os texels no infinito — mas **não para a
posição**, onde o argumento é idêntico e a consequência pior. Escritas em
`oPos` são mascaradas (`write_masked(opos.v, src, omask)`, linha 391), então
um programa que escreve só `oPos.xy` deixa `w` no valor inicial. Com `w = 0`
todo vértice lia como atrás do olho e o teste de plano próximo descartava o
frame inteiro.

**Fix**: `opos.v[3] = 1.0f` após o memset.

Por que é seguro independente do resto: se o programa do título escrever
`oPos.w`, a escrita sobrepõe o default e nada muda; se não escrever, `0` é
certamente errado e `1` é o default documentado. Só pode agir no caso em que
hoje está errado.

### Lição de método: log que não identifica o próprio binário não é evidência

Duas execuções foram comparadas para decidir se o default de `oPos.w`
resolvia, e **nenhuma das duas dizia qual build estava rodando**. O
desacoplamento do `DMA_PUT` (build 14:20) e o fix do `oPos.w` (build 14:27)
eram builds distintos; as duas execuções tinham o mesmo module base e
nenhuma com spam de `DMA_PUT`, o que é compatível com ambas serem a de
14:20 — isto é, com o fix **nunca tendo sido testado**. A conclusão
"hipótese falsificada" foi registrada antes de verificar isso, e estava
mal fundamentada.

Fix de processo: `nv2a_vsh_run()` agora imprime uma linha, uma vez, sem
depender de variável de ambiente:

```
[VSH] interpreter built <data> <hora>, oPos.w default 1.0
```

`__DATE__`/`__TIME__` são da própria unidade de tradução do interpretador,
que é o certo — mexem quando o interpretador muda. E o valor reportado vem
da macro `NV_OPOS_W_DEFAULT`, a mesma usada na atribuição, para o relato
não poder divergir do comportamento.

Segundo erro do mesmo ciclo: o `Diagnose (Vertex).cmd` foi atualizado aqui
para ligar `RECOMP_VSH_DUMP`, mas o usuário já tinha copiado a versão
anterior, e não foi avisado de que o `.cmd` mudara junto com o exe. O log
voltou com `[VTRACE]` e **zero** linhas `[VSH]` — e `dump_program()`
imprime o cabeçalho antes de qualquer loop, logo a ausência total prova que
`getenv("RECOMP_VSH_DUMP")` era NULL, não que o programa estava vazio.
**Ao entregar binário + launcher, dizer explicitamente que os dois mudaram.**

### Status da hipótese do default de `oPos.w` (em aberto)

Testado: com `opos.v[3] = 1.0f`, **continua 100% descartado** (2.262.208
triângulos atrás do olho, 0 desenhados, `x 0..0 y 0..0`).

O binário novo estava rodando de fato — prova independente: o spam de
`DMA_PUT` desapareceu do log, ou seja o desacoplamento do
`s_nv2a_put_trace` entrou. Então o `w` chega em `0` na saída **mesmo com o
default em 1**, o que só pode significar que **o programa do título escreve
`oPos.w` e escreve zero**.

A mudança foi mantida (é o default `(0,0,0,1)` documentado, consistente com
as linhas vizinhas de cor/texcoord, e inofensiva), mas o comentário no
fonte foi corrigido para não afirmar que resolveu nada. A causa está na
**execução do programa**, não nos defaults.

Pista aritmética do que o programa faz: `out.x == in.x` exatamente e
`out.y == in.y + 16`, com `out.z = out.w = 0`. Isso é compatível com
`oPos = v0 * c[k] + c[j]` onde `c[k] = (1,1,0,0)` e `c[j] = (0,16,0,0)` —
daria `w = 1*0 + 0 = 0`. Se for isso, ou a constante lida está errada, ou o
interpretador está executando os slots errados (suspeita secundária:
`s_start_slot` / detecção do bit `FINAL` em `field(ins, 3, 0, 1)`).

### Próximo passo: `RECOMP_VSH_DUMP`

`RECOMP_VSH_DUMP=1` (`nv2a_vsh_interp.c:319` → `dump_program()` na 138) já
está compilado no exe e **não precisa rebuild**. Imprime a desmontagem de
cada programa distinto que o título carrega: opcode MAC/ILU, os três
operandos de origem, registrador de destino + máscara de escrita, e qual
slot é `FINAL`. Isso diz se `oPos.w` vem de uma instrução real (e de qual
constante/registrador) ou se o interpretador está rodando os slots errados.

Ligado no `Diagnose (Vertex).cmd`.

### Sobre a versão PAL (pergunta do usuário)

A região **importa e já custou um bug** — foi exatamente a causa raiz do
fence (`0x0035FB48` NTSC-US vs. `0x0035FEE8` no nosso PAL). Endereços
hardcoded são o risco real. Mas **não é a causa do `w = 0`**: o programa de
vértice vem dos dados do próprio jogo e o interpretador é agnóstico de
região. O `clip 640x448+0+16` com framebuffer 640×480 também é coerente,
não é anomalia de PAL.

### Pendências menores notadas no mesmo log

- Um **segundo subsistema de log** (`[THUNK]`/`[FILE]`/`[XBOX]`/`[HAL]`,
  com timestamps) reporta `Thunk table: 147/378 resolved, 231 unresolved` e
  centenas de `Unresolved kernel ordinal 0`, enquanto o log principal diz
  `147/147 resolved`. Os dois contam coisas diferentes (378 = tamanho da
  tabela, 147 = ordinais realmente importados pelo XBE); os "ordinal 0" são
  slots vazios. **Não é o bloqueador**, mas o alarme `may crash!` é
  enganoso e vale silenciar.
- `ExQueryNonVolatileSetting: unhandled index 0x103 / 0x104` — configuração
  de vídeo/idioma não modelada.
- `Tracks\tlist.bin` não existe no dump (`ERROR_FILE_NOT_FOUND`), e
  `TDATA\FFFE0000\MUSIC\ST.DB` também não. O primeiro pode importar para
  carregar pistas.
- `[GPU] 835.75 fps` sem nada desenhado confirma que o custo real está no
  rasterizador, não no resto.

## Nota operacional: o loop de espera que nunca terminava

Os "shells abertos" que ficavam pendurados tinham causa real, não era só
eu esquecer de fechar: o loop

```bash
until ! pgrep -f "cmake --build build-mingw" >/dev/null; do sleep 15; done
```

**casa com a própria linha de comando dele** — `pgrep -f` vê o `zsh -c`
que contém a string, então a condição nunca fica falsa e o loop roda pra
sempre mesmo com o build já terminado. Padrão correto: um único job de
background por tarefa longa e esperar a notificação dele, sem loop de
polling.

## Se isso não resolver sozinho

Esse fix deve destravar o `sub_00351B10`, mas pode expor outro endereço
"calibrado pra NTSC" equivalente em outro lugar do boot (o comentário
original já avisava "Same layout as HL2" pra pelo menos duas constantes
—vale `grep -rn "0x35F\|0x360\|calibrat" src/` no `main.c` se aparecer
outra trava parecida, antes de sair caçando do zero de novo).
