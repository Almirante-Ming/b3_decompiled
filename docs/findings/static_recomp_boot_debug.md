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

## O microcódigo, finalmente: `oPos.w = c[191].w`

Com o build stamp confirmando o binário (`[VSH] interpreter built Oct 14
2026 14:37:45, oPos.w default 1.0`) e o `w` ainda saindo 0, o default está
descartado **com prova**. O `RECOMP_VSH_DUMP` deu o programa inteiro — são
só 6 instruções:

```
0 add  A=v0.xyzz  C=c[191].xyzz      outo0 mE   -> oPos.xyz = v0 + c[191]
1 mov  A=c[191].xyzw                 outo0 m1   -> oPos.w   = c[191].w
2 mul  A=R12.zzzz B=c[58].zzzz       outo0 m2   -> oPos.z   = oPos.z * c[58].z
3 mad  A=R12.zzzz B=R1.xxxx C=c[59]  outo0 m2   -> oPos.z   = ...
4 mov  A=v1                          outo9 mF  -> texcoord0 = v1
5 mov  A=v2                          outo3 mF  -> cor       = v2   FINAL
```

Leitura das máscaras: `mE`=xyz, `m1`=w, `m2`=z, `mF`=xyzw (a convenção
oposta, bit0=x, daria "escreve yzw e depois x", que não faz sentido para um
shader de posição — esta é autoconsistente).

**A instrução 1 é a causa: `oPos.w = c[191].w`.**

E `R12` é alias de `oPos` no NV2A (confirmado em `nv2a_pb_exec.c:372`:
`tdst == 12 ? opos.v`), o que explica `z = 0`: as instruções 2 e 3
multiplicam `R12.z`, que já vinha 0 da instrução 0.

### Onde `c[191]` é carregada

- `nv2a_pb_exec.c:3220/3224` carregam **só** c[59] e c[58], dos métodos
  `0x0A20`/`0x0AF0` (viewport offset/scale).
- O caminho geral existe: `0x1EA4` (`_CONSTANT_LOAD`, define o índice) +
  `0x0B80..0x0C00` (`TRANSFORM_CONSTANT`, streaming), servidos por
  `nv2a_vsh_set_constant_load()` e `nv2a_vsh_constant_word()`
  (`nv2a_vsh_interp.c:69-83`). O streaming parece correto: grava 4 words e
  avança o índice.
- Um programa também pode escrever constante (`nv2a_vsh_interp.c:410`,
  `write_masked(s_const[oaddr], ...)`).

### Cuidado: o valor de `c[191]` estava sendo *inferido*, não lido

Da instrução 0 dá para deduzir `c[191] = (0, 16, 0, ?)` — `167.947 =
151.947 + 16`, e o 16 casa com o `+0+16` do surface clip. Mas isso é
aritmética reversa, não medição, e a lista de constantes do `VTRACE`
(`58, 59, 96, 97, 112-115`) **não incluía 191**.

Adicionada (190 e 191). O próximo log separa dois culpados bem diferentes:

| `c[191]` lido | conclusão |
|---|---|
| `(0, 16, 0, 0)` | o upload realmente não traz o `w` → bug no caminho de constantes / outra rota do driver |
| `(0, 16, 0, 1)` | o upload está certo e o **interpretador lê errado** (swizzle ou índice em `read_src`, `nv2a_vsh_interp.c:211`) |

### Medido

```
[VTRACE]   c[190] 0.003125 -0.00446429 0 0
[VTRACE]   c[191] 0 16 0 0
```

Primeira linha: `(0, 16, 0, **0**)` confirmado por leitura. O `w` é zero no
banco de constantes.

`c[190]` dá o contexto que faltava: `0.003125 = 1/320` e
`-0.00446429 = -1/224`, o **inverso** do viewport scale (`c[58] = 320,
-224`). Então 190/191 são o par de espaço de tela que o runtime D3D monta —
190 escala inversa, 191 offset.

**Bônus: a convenção de máscara agora está provada, não suposta.** Com
`c[191]` conhecido, a leitura alternativa (bit0=x) daria
`oPos.x = c[191].x = 0`, e o observado é `268.091`. Só `mE`=xyz / `m1`=w
fecha com os dados.

### Por que não hackear `c[191].w = 1` direto

Seria uma linha e provavelmente faria a imagem aparecer. Mas se o título
**escreve** esse `w` e nós estamos perdendo a escrita, o mesmo bug está
corrompendo outras constantes em silêncio — e o hack esconderia isso.

Verificado antes de instrumentar: `nv2a_vsh_set_constant()`
(`nv2a_vsh_interp.c:85`) **não tem nenhum chamador**, então a rota de
streaming (`0x1EA4` + `0x0B80..0x0C00`) é a única entrada possível.

Instrumentação adicionada em `nv2a_vsh_set_constant_load()` e
`nv2a_vsh_constant_word()`, sob o mesmo `RECOMP_VSH_DUMP`, com teto de
prints (constantes são recarregadas por batch):

```
[VSHC] load index <n>
[VSHC] c[<n>].<comp> = <float> (raw <hex>)
```

### Resultado: o título escreve `w = 0` de propósito

```
[VSHC] load index 190
[VSHC] c[190].x = 0.003125 (raw 3B4CCCCD)
[VSHC] c[190].y = -0.00446429 (raw BB924925)
[VSHC] c[190].z = 0 (raw 00000000)
[VSHC] c[190].w = 0 (raw 00000000)
[VSHC] c[191].x = 0 (raw 00000000)
[VSHC] c[191].y = 16 (raw 41800000)
[VSHC] c[191].z = 0 (raw 00000000)
[VSHC] c[191].w = 0 (raw 00000000)
```

Oito words a partir do índice 190 — quatro para cada constante, **nenhum
perdido**. Não havia bug de upload: o banco está fiel e o valor é zero
porque o título manda zero.

Logo o Xbox real renderiza isso com `w = 0`, e o errado é a **nossa
interpretação**.

### Causa raiz: `w = 0` é "sem projeção", não "atrás do olho"

O zero é estrutural, não acidente: a instrução 1 computa
`R1.x = RCC(c[191].w)` exatamente para o caso geral em que esse `w` é real.
Aqui, com `w = 0` e `v0.z = 0`, o resultado colapsa em `z = 0` por desenho.

Duas evidências independentes fecham o quadro:

- `SET_CLIP_MIN`/`SET_CLIP_MAX` (`0x0394`/`0x0398` = `0` e `16777215`) são o
  recorte de **z** em unidades de profundidade. Não há teste de plano
  próximo em `w` no hardware para modo programa.
- O comentário do próprio `raster_xf_clipped` registra que no 3D **o
  programa** divide por `w` e deixa `oPos.w` intacto (é por isso que o
  runtime desfaz a projeção para recortar). Ou seja: o hardware não divide;
  quem projeta é o shader.

Então no 3D `w <= 0` é de fato "atrás do olho". Neste shader 2D, `w = 0` é
"**nunca projetado**" — e o teste de plano próximo lia uma coisa como a
outra, descartando todo triângulo de todo batch 2D. Era a tela preta.

**Fix** (`raster_xf_clipped`, `nv2a_pb_exec.c`): `w` exatamente zero nos
**três** vértices → rasteriza com `w = 1` (interpolação afim, sem divisão) e
não recorta. Discriminador seguro: vértice 3D cair exatamente no plano do
olho é medida-zero, e se vier misturado com `w > 0` o recorte normal
continua rodando.

Contado em separado e reportado no resumo
(`%u unprojected (w=0, drawn affine)`) para a heurística nunca engolir em
silêncio um batch que deveria ter sido recortado.

## Imagem na tela — e o "disco sujo" era diagnóstico correto do jogo

Com o fix, a UI 2D renderiza. O jogo então exibe mensagem de disco
danificado, e o log já apontava o motivo antes da tela aparecer:

```
[PATH] \Device\CdRom0\Tracks\tlist.bin
[FILE] -> 0xC0000034 FAILED (win32 err=2 ERROR_FILE_NOT_FOUND)
```

`err=2` (arquivo) e não `err=3` (caminho): o diretório resolveu, o arquivo
faltou. O jogo pede a lista de pistas, não acha, e conclui — corretamente —
que a mídia está incompleta. **O erro era nosso, não dele.**

### Bug no nosso extrator: diretório XDVDFS é árvore, não lista

Um diretório XDVDFS é uma **árvore binária**: cada entrada carrega os
offsets das subárvores esquerda e direita (em unidades de 4 bytes, a partir
do início dos dados do diretório; 0 = sem filho). Entradas não cruzam
fronteira de setor, então a cauda de cada setor é preenchida com padding —
e é exatamente por isso que varredura linear não funciona.

O `read_dir()` em `tools/xbe/xdvdfs_extract.py` varria linearmente e
**parava** (`break`) no primeiro byte de padding — o mesmo `break` que eu
havia introduzido para resolver o crash de "embedded null byte". A correção
de um sintoma mascarou a causa: ignorar `left`/`right` truncava todo
diretório cujas entradas passassem de um setor.

Confirmado no ISO antes de mexer: a entrada existe e é única —
`left=0 right=545 sector=1683860 size=4096 attr=0x80`. Os 4096 bytes batem
com o `want=4096` do log.

**Fix**: travessia da árvore com pilha explícita e conjunto `seen` (imagem
corrompida pode apontar subárvore para si mesma, e recursão sobre bytes não
confiáveis não deve travar a ferramenta). Nome inválido agora **abandona o
ramo** em vez de parar tudo — a diferença entre perder uma subárvore e
perder o resto do diretório.

Medido antes/depois no disco inteiro: 56 diretórios, **793 → 816 entradas**,
um único diretório truncado (`tracks`: 76 → 99). Mas uma das 23 entradas
perdidas era o subdiretório **`US`**, com as pistas de crash junction
(C1_V1, C1_V2, C2_V2, C3_V1, C3_V2) — em cascata, **121 arquivos e 780 MB**.
Também faltavam `_EATrax0/1.xwb` (86 MB cada), `_FEMain.xmv`, `_femain.rws`
e os bancos de voz do DJ.

**Extração agora idempotente**: arquivo existente com o tamanho exato é
pulado. Re-rodar após corrigir um bug de parsing custa só o que faltava, e
re-rodar é o uso normal da ferramenta — bug de parsing só é descoberto
*depois* de uma extração.

**Verificação final**: 761 arquivos, 2.841.931.784 bytes, **zero
divergências** de presença ou tamanho. Antes faltavam ~840 MB.

### O que o trace de upload distinguiria (mantido para referência)

- **nenhum `[VSHC] c[191].w`** → a escrita acontece fora da nossa
  visibilidade (ex.: no init do D3D, antes do executor seguir o
  pushbuffer). Aí o certo é semear o valor na inicialização, não remendar.
- **`c[191].w = 0` explícito** → o título mesmo manda zero, e a semântica
  de `w` no NV2A para batch de espaço de tela é que precisa ser
  reinterpretada.
- **`c[191].w = 1` escrito e lido como 0** → bug real de escrita/leitura no
  banco, e o impacto é bem maior que esta tela.

### Status da hipótese do default de `oPos.w` (fechada: não era)

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

## Tela de loading: não é travamento, é espera ocupada no "GPU idle"

Com o fix do `w`, **pixels são escritos**: `20740908 pixels`, `95160
triangles rasterised`, `0 dropped behind the eye`, `123184 unprojected`,
`brightest pixel 0xFF9EC4F7`. A UI 2D renderiza.

`[RAST] 4 raster threads (8 cores, default cores-4)` — threading está
ligado, não é o gargalo. E os `indirect calls` crescem firme (+2300~2500
por 3s), draws e fence avançam: **não está travado**, está girando.

### Onde, por símbolo (não por chute)

Ordinais que crescem: **129 `KeRaiseIrqlToDpcLevel`**, **161 `KfLowerIrql`**,
**294 `RtlLeaveCriticalSection`** — polling sob IRQL elevado.
`151 KeStallExecutionProcessor` congelou em 506.

Pilhas resolvidas com `nm` + delta de ASLR (base `0x7FF7617B0000`):

```
tid 15268 (jogo, 17s cpu)   sub_001D1BD8 -> sub_001D2B53 -> sub_00156740
                            -> sub_001D97E0 -> sub_001DEBA0 -> sub_001DDEB0
                            -> sub_00351430 -> sub_003513F0
                            -> sub_00351830 -> sub_00351DD0
tid 18652 (100% de 1 core)  nv2a_ack_thread -> nv2a_pb_scan -> pb_walk
                            -> nv2a_pb_exec_method -> draw_primitive
                            -> raster_xf_clipped -> raster_xf_triangle
                            -> xf_rows -> nv2a_rc_eval -> read_rgb
```

`sub_00351430 -> sub_003513F0` são **as mesmas** funções da trava original do
fence — mesma região do D3D8LTCG, folha diferente (`sub_00351DD0` em vez de
`sub_00351B10`).

### O loop, lido no C recompilado

`src/game/recomp/gen/recomp_0024.c:4850`:

```c
ecx = MEM32(0x35FEE8);        // ponteiro do device D3D
eax = MEM32(ecx + 0x934);     // base do bloco de registradores NV2A
loc_00351E10:
    if (MEM32(eax + 0x3240) != MEM32(eax + 0x3244)) goto loc_00351E10;
    if (MEM32(eax + 0x400700) != 0)                 goto loc_00351E10;
MEM32(edx + 8) |= 0x2000;     // marca idle
```

`0x3240`/`0x3244` = `PFIFO_CACHE1_DMA_PUT`/`DMA_GET`; `0x400700` =
`PGRAPH_STATUS`. É "espera a GPU ficar ociosa".

### Por que isso pode nunca sair

Nós espelhamos `NV2A_USER_DMA_PUT/GET` em **`0x800040`/`0x800044`**
(`xbox_memory_layout.c:241`). `0x3240`/`0x3244` aparecem **só num
comentário**, e `0x400700` **não existe no código**.

Com a aperture zerada os três leriam 0 e o loop sairia na hora. Então a
suspeita é o **endereço base**: se `MEM32(device + 0x934)` nunca foi
inicializado e vale 0, `+0x400700` cai no VA 0x400700 do Xbox, **dentro de
`.data` do jogo** (0x003B2720..0x0076BD1C) — valor arbitrário, e gira para
sempre. Mesmo padrão do bug do fence.

A tabela `NV2A_ACK` (6 entradas) já prevê isso no comentário do autor:
*"extend as more handshakes turn up. A spin on a bit that is not listed
still hangs -- run the title and the watchdog sample will name the
register."*

**Não estendi a tabela ainda**, porque estender só ajuda se o base estiver
certo. Instrumentei a cadeia (`gpu_idle_trace`, sob o `RECOMP_FENCE_TRACE`
que o launcher já liga):

```
[GPUIDLE] dev=0x... regbase=0x... put=... get=... pgraph=...  -> idle|SPINNING
```

- `regbase` dentro de `0xFD000000..0xFE000000` → a aperture está certa e o
  problema é outro (algo escreve valores diferentes ali).
- `regbase` = 0 ou fora da aperture → é o campo `device+0x934` que ninguém
  preenche, e o fix é semeá-lo, não remendar a tabela de ack.

## A tela de loading: PFB flush em `sub_00351F70` (14/out)

O jogo **não** estava travado no laço que eu vinha consertando. Ele terminou
de carregar — `Global.txd`, `GlobalEn.bin`, `HeadEn.bin`, `pveh/vlist.bin`,
`Tracks/tlist.bin`, `Data/PrgData.bin`, `Graphics/0.bum`…`16.bum`,
`sound/generic.awd` — e entrou num estado estável desenhando a 114 fps.

### Como foi localizado

O watchdog imprime endereços de host, e o `[BASE]` dá o delta de ASLR. O exe
não é stripped, então os quadros resolvem:

```
link = host - 0x7FF701F10000 + 0x140000000

0x7ff703114dd0 -> 0x141204dd0  sub_00351DD0+0x1d0
0x7ff70311525f -> 0x14120525f  sub_00351830+0x38f
0x7ff703117ed2 -> 0x141207ed2  sub_003513F0+0xc2
0x7ff70315e79f -> 0x14124e79f  sub_00351430+0x3df
```

Todas as 23 amostras pegaram a thread principal em `+0x1d0` ou `+0x1d6` —
seis bytes de distância, um laço de duas instruções:

```
141204dd0:  testl $0x10000,(%rax)
141204dd6:  jne   141204dd0
```

O C gerado de `sub_00351DD0` não tem `0x100410` em lugar nenhum: o GCC
inlinou `sub_00351F70`, chamada em `loc_00351DE7`.

```c
void sub_00351F70(void) {
    eax = MEM32(0x35FEE8);        // ponteiro do device
    eax = MEM32(eax + 0x1C28);    // base de registradores cacheada
    ecx = MEM32(eax + 0x100410) | 0x10000;
    MEM32(eax + 0x100410) = ecx;  // chuta o flush do PFB
loc_00351F90:
    if (MEM32(eax + 0x100410) & 0x10000) goto loc_00351F90;
}
```

### Por que a tabela `NV2A_ACK` não resolvia

`0x100410` **já era** a primeira entrada da tabela. O problema é o endereço: a
thread de ack limpa via nosso ponteiro de aperture, em `0xFD100410`, e o título
não assume a base da aperture — ele usa a que cacheou em `device+0x1C28`. Se
esse campo não foi semeado, o jogo seta o bit em `0x100410`, dentro dos 64 MB
de RAM comum, onde nada o limpa. Spin eterno, sem falha, sem log.

`device+0x1C28` não é escalar: é o endereço de uma subestrutura passada para
`sub_00356430` dentro de `sub_00353700` (a mesma init que escreve `+0x934`), e
quem preenche o primeiro dword está mais fundo na cadeia
(`sub_00356590` / `sub_003565BB`). `+0x934` e `+0x1C28` são campos distintos:
`+0x934` alimenta a espera de DMA e mediu `0xFD000000`; isso não diz nada sobre
o outro.

### Ordem dos portões dentro de `sub_00351DD0`

1. `loc_00351DE7` → flush do PFB (`0x100410`, bit `0x10000`) — **este**
2. `loc_00351E10` → `[base+0x3240] == [base+0x3244]` (PFIFO CACHE1)
3. `loc_00351E1E` → `[base+0x400700] == 0` (PGRAPH_STATUS)

Os dois últimos já estavam corrigidos (espelho do par PFIFO e PGRAPH na tabela
de ack). São necessários em sequência, um atrás do outro — consertar o segundo
não podia mostrar efeito nenhum enquanto o primeiro barrava.

### Erro meu, para não repetir

O `[GPUIDLE]` reportou `regbase+0x3240/+0x400700 unreadable` e eu li isso como
um achado. Não era: `fence_readable()` validava só a RAM principal e a janela
contígua, e negava a aperture de MMIO — que é mapeada e perfeitamente legível,
em `XBOX_NV2A_BASE + g_memory_offset`. O predicado estava errado, não a
aperture. Um diagnóstico que mente "ilegível" sobre memória legível é pior do
que não reportar nada, porque parece resultado; custou uma rodada inteira atrás
de uma aperture que nunca esteve errada. Corrigido na própria
`fence_readable`, não no ponto de uso, para que nenhuma outra sonda herde a
mesma mentira.

## Correcao: o campo `+0x1C28` estava semeado (14/out, depois)

Medido, e refuta a secao anterior:

```
[GPUIDLE] dev=0x0035DA40 regbase=0xFD000000 pfbbase=0xFD000000
          flush=0x00010000 BUSY  put==get  pgraph=0x00000000
```

`device+0x1C28` contem `0xFD000000`. O endereco do flush sempre esteve
correto, nenhum aviso `[PFB]` foi impresso, e os tres portoes de DMA estao
abertos. A hipotese do "campo nunca preenchido" estava errada.

O que o log mostra, relido: nos **limpamos** o bit naquele endereco, na mesma
thread que mais adiante o le como `BUSY`. Isso nao e deadlock -- e o titulo
setando o bit outra vez. A thread de ack limpava o bit uma vez por iteracao,
e a iteracao carregava o scan do pushbuffer e o executor que rasteriza o
frame: ~9 ms a 110 fps. O titulo ficou **serializado na nossa latencia de
servico**.

Na pratica isso significa que havia dois problemas sendo tratados como um:

1. **Acoplamento de latencia.** Nada em `NV2A_ACK`/`NV2A_IDLE` depende do
   pushbuffer, do executor ou do frame, e enquanto moravam na mesma thread a
   resposta chegava uma vez por frame. Hardware limpa esses bits em
   microssegundos, nunca na cadencia de um frame, entao o acoplamento era o
   defeito. Agora tem thread propria, com `Sleep(0)`: o esperador ja esta
   queimando um core num laco de duas instrucoes em outro core, e ceder basta
   para a latencia cair de um frame para um quantum.

2. **O bloqueio real, ainda em aberto.** Com os arquivos todos carregados e os
   portoes de DMA abertos, resta descobrir o que o titulo espera. Pista nova:
   o ordinal 99 e `KeDelayExecutionThread` e cresce a ~10/s em regime estavel
   -- a thread principal dorme dez vezes por segundo, o padrao de um laco
   "espera e tenta de novo" consultando uma condicao a 10 Hz.

### O contador que separa os dois

`clears=` na linha `[GPUIDLE]` conta quantas vezes limpamos o bit do flush.
"O bit esta setado" num instante de amostragem nao diz se o titulo esta preso
num unico flush ou completando milhares deles, e os dois pedem correcoes
opostas:

- `clears=1` (ou punhado) -> nossa escrita nao alcanca a memoria que o titulo
  le, e o problema e de endereco/mapeamento.
- `clears` nos milhares -> alcanca, e o que parecia travamento era o titulo
  correndo na velocidade em que o atendiamos.

### Licao

Eu diagnostiquei "esta na funcao X esperando o bit Y" e tratei como prova de
que Y nunca era satisfeito. A posicao da thread diz onde ela espera, nao se a
espera termina. Para distinguir e preciso contar eventos ao longo do tempo,
nao amostrar estado.

## Regressao: o acking so era seguro porque era lento (14/out)

Tirar os bits de ocupado da thread de ack e dar a eles uma thread propria
**quebrou o titulo inteiro**. Medido:

```
clears=0   flush=0x00000000 clear      <- o bit nunca nem foi setado
indirect calls 41590 (+0)              <- congelado de verdade, nao devagar
[GPU] 0.00 fps (8 flips), 21 draws
*fence=0x0000002B put=0x0000002B       <- congelado
```

Travou **antes** do flush, com um unico arquivo lido (`Global.txd`), e numa
funcao diferente: `sub_00351AA0+0xb7`, nao `sub_00351DD0+0x1d0`.

### Mecanismo

`NV2A_ACK` zera `PGRAPH_STATUS` sob mascara `0xFFFFFFFF` e `NV2A_IDLE` forca
bits. Na cadencia de um frame, o titulo confiavelmente le de volta o que
escreveu antes de sobrescrevermos. Na cadencia de uma thread com `Sleep(0)`,
nao le. **A janela de valor obsoleto era de onde o titulo lia as proprias
escritas** -- o acking era seguro por ser lento, e acelerar transformou isso
num clobber imediato.

Revertido. O unico residuo da tentativa e o contador `clears=`, e o diff
contra o estado bom confirma que nenhuma mudanca de comportamento sobreviveu.

### `sub_00351AA0`, para referencia

```c
loc_00351AB0:
    eax = MEM32(edx + 0x400B10);   // edx = device+0x934 = 0xFD000000
    ecx = MEM32(edi) << 2;         // edi = device+0x30 = ponteiro do fence
    ecx = ecx ^ eax;
    if (ecx & 0x7C) goto loc_00351AB0;
```

`0x400B10` nao esta em nenhuma das tabelas, entao le zero da aperture e o XOR
e identidade. A condicao de saida vira `(fence << 2) & 0x7C == 0`, ou seja
`fence & 0x1F == 0`: o fence tem de cair num multiplo de 32. No log ele esta
congelado em `0x2B` (`& 0x1F = 0x0B`), insatisfazivel -- mas congelou porque o
titulo parou de submeter, nao o contrario. O travamento aqui e consequencia;
a causa esta acima. Com o fence em movimento (ele avanca de 4 em 4) a condicao
e satisfeita periodicamente, e nas execucoes boas era.

### Licao

A correcao certa para a latencia nao e reconhecer o bit mais rapido, e **a
escrita nunca depositar o bit** -- do jeito que os bits de reset do AC97 ja
sao tratados neste arquivo, com a pagina em PAGE_READONLY e o handler
mascarando. O comentario do AC97 ja dizia que a abordagem da tabela e
inadequada para isso; eu nao o tinha lido como aviso.

## O PFB flush era pista morta: o contador provou (14/out)

`clears=` foi 0 -> 1 -> 2 -> ... -> 8 em 30 segundos. Nem zero, nem milhares:
~0,27/s, contra ~110 passagens por segundo da thread de ack. Tivemos ~3300
oportunidades e achamos o bit setado 8 vezes.

A razao: `NV2A_ACK[0]` e `{ 0x100410, 0x10000 }` e limpa em
`regs + 0x100410` = `0xFD100410`. O `pfbbase` medido e `0xFD000000`, logo
`pfbbase + 0x100410` e **a mesma palavra**. A tabela sempre limpou no lugar
certo; o `pfb_flush_ack_tick` que eu escrevi era um duplicado que perdia a
corrida para a propria tabela. As 8 contagens sao essas corridas.

Consequencias:

- O flush **nunca** foi o bloqueio. O titulo o cicla uma vez por frame a
  110 fps e nao fica preso nele -- a thread principal esta naquele laco porque
  e ali que ela espera o frame.
- As tres builds gastas nisso (espelho PFIFO, PGRAPH na tabela, tick do PFB)
  nao podiam ter efeito. O espelho PFIFO e o PGRAPH seguem corretos pelos seus
  proprios motivos; o tick foi removido, e o que ficou e um comentario na
  entrada da tabela registrando a medicao, porque "o titulo usa outra base" e
  o tipo de historia plausivel que custa um dia.

### Estado atual, medido

O jogo **progride**: `indirect calls +2766/3s`, draws subindo, fence
avancando, 110 fps, todos os arquivos pedidos lidos. Ele so nao sai da tela
de loading. Portoes de DMA todos abertos (`put == get`, `pgraph = 0`).

### Instrumento novo

`[SLEEP]` imprime o endereco de retorno e o intervalo de cada **call site
distinto** de `KeDelayExecutionThread` (ordinal 99), uma linha por site, nao
por chamada. Esse ordinal corre a ~10 Hz em regime estavel, que e a forma de
um laco "espera e tenta de novo"; o endereco de retorno nomeia a funcao que
espera, e o C recompilado dela declara o teste. A contagem sozinha nunca ia
dizer qual condicao e.

### Pista secundaria anotada

`[READ] from=0x0033BFC8 @4608 want=512 got=0 st=0xC0000011` -- leitura de
`partition0` em 4608 retorna EOF (o `@4096` passou a funcionar nesta
execucao). O chamador esta em `XNET+0x2a48`, componente de rede/cache, entao
provavelmente nao e o bloqueio da tela de loading. Nao perseguido.

### Licao

"A thread esta na funcao X esperando o bit Y" nao prova que Y nunca e
satisfeito, e eu repeti esse erro tres vezes seguidas no mesmo bit. O que
separa travado de estrangulado de normal e **contar eventos ao longo do
tempo**; amostrar estado nao serve. O contador custou vinte linhas e matou
uma investigacao inteira que amostragem tinha sustentado por tres builds.
