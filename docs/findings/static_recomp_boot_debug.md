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

## Loop fechado: build, instalacao e execucao locais (14/out)

O jogo esta exposto em `/mnt/a/burnout3` (`A:\burnout3` no Windows) e a
interop do WSL funciona, entao da para compilar, instalar e executar sem
depender de troca de logs:

```sh
cp .../bin/burnout3.exe /mnt/a/burnout3/bin/
timeout -k 5 30 /mnt/c/Windows/System32/cmd.exe /c \
  "cd /d A:\\burnout3 && set RECOMP_PB_EXEC=1&& bin\\burnout3.exe" > run.log 2>&1
/mnt/c/Windows/System32/taskkill.exe /IM burnout3.exe /F
```

O `timeout` mata o `cmd.exe` mas nao o neto, dai o `taskkill` explicito.

## O sleep de 10 Hz e o worker de cache de disco (nao e o bloqueio)

`[SLEEP]` achou exatamente um call site. A cadeia levou cinco tentativas,
e as quatro primeiras falharam de formas que valem registro:

1. `BRIDGE_MEM32(g_esp)` dentro de um bridge da o **argumento zero**, nao o
   endereco de retorno: o dispatcher ja fez o pop. Imprimiu `wait_mode` (1)
   como call site, que parece endereco corrompido e nao endereco errado. O
   dispatcher ja guarda o valor certo em `g_xbox_kernel_caller`.
2. A cadeia de `ebp` termina depois de um salto: os wrappers de sleep acima
   de `0x001D1976` sao FPO leaves e nunca empilham `ebp`.
3. `CaptureStackBackTrace` devolve zero frames -- as funcoes recompiladas nao
   tem unwind info utilizavel.
4. `StackWalk64` com `RtlCaptureContext` tambem devolve zero.
5. Varredura conservadora da pilha do guest, filtrando o que cai em `.text`.
   Funcionou -- depois de corrigir o limite, que eu havia fixado na pilha da
   thread principal (`0x780000..0xF80000`) enquanto quem dorme e um worker com
   pilha no heap. O laco saia na primeira iteracao e imprimia nada, o que
   parece cadeia vazia e nao limite errado.

Resultado:

```
[SLEEP] tid 11360, KeDelayExecutionThread from 0x001D19A7, 50.000 ms
        <- 0x001D19E5 <- 0x0021CBFF <- 0x001D1C4A <- 0x00244B88 <- 0x0021CBE0
```

`sub_0021CBE0` (a funcao vizinha declara `Source: driver/xbox/xbcache.c`):

```c
MEM32(0x3C61E4) = 1;                  // "estou rodando"
if (MEM32(0x41D384) == 0) goto fim;
laco: sub_0021CB00(); Sleep(0x32);    // 50 ms
      if (MEM32(0x41D384) != 0) goto laco;
fim:  MEM32(0x3C61E4) = 0;
```

E o worker de cache de disco, fazendo o trabalho dele. Os dois flags
(`0x41D384`, `0x3C61E4`) aparecem apenas em `recomp_0006.c`, o modulo de
cache -- e handshake interno de start/stop, nao portao da tela de loading. O
sinal de 10 Hz esta explicado e **eliminado como suspeito**.

## Defeito real corrigido: `Partition0.img` truncado

`saves/Partition0.img` tinha **4608 bytes = 9 setores** exatos. O jogo le o
setor 9 (offset 4608) e recebia `STATUS_END_OF_FILE`. Numa particao raw uma
leitura dentro do dispositivo nunca devolve EOF.

Estendido com zeros para 1 MB (backup em `Partition0.img.bak-4608`).
Resultado medido: **zero EOFs na execucao inteira**, e `@4608` passa a
devolver 512 bytes. Nao destravou a tela de loading -- nenhum arquivo novo
foi aberto, mesma geometria -- mas era defeito de verdade e saiu da mesa.

O conserto proprio e no codigo, nao no arquivo: `xbox_NtReadFile` devolve
`STATUS_END_OF_FILE` quando a leitura cai inteira apos o fim, o que esta
certo para um arquivo e errado para uma imagem de particao. Distinguir os
dois exige marcar os handles de imagem em `NtCreateFile` e limpar em
`NtClose`. Nao feito: ~40 linhas na camada de arquivos com questao de
lifetime, para um defeito que nao e o bloqueio, e mudanca apressada nessa
camada e exatamente o que causou a regressao do acking.

## Onde o bloqueio nao esta

Eliminados por medicao, nao por argumento: PFB flush, portoes de DMA
(`put == get`, `pgraph == 0`), fence, o sleep de 10 Hz, e o EOF de
`partition0`. O jogo roda um laco estavel a ~120 fps, le todos os arquivos
que pede, e a thread principal passa cada frame no caminho de present do D3D.
Nada disso e o problema.

## O bloqueio: handoff de dois eventos com acordar perdido (14/out)

Primeiro, olhar a tela em vez de deduzi-la. `RECOMP_FB_DUMP=fb\f` com
`RECOMP_NV2A_TRACE=1 RECOMP_PB_REPORT_MS=2000` escreve um BMP a cada 2 s.

A tela renderiza **corretamente**: "LOADING / LADEN / CARGANDO / LADDAR"
(PAL multi-idioma) e uma barra de progresso. Minha suspeita sobre textura
estava errada -- o texto sai perfeito.

A barra vai de 83px para 121px de 160 nos primeiros ~2 s e **para em 75,6%**.
Do segundo frame em diante o framebuffer e byte-a-byte identico (mesmo md5
nos 11 frames seguintes). Nao e lentidao: e parada.

### Quem esta parado

`[WAITLONG]` fatia esperas INFINITE em pedacos de 2 s e denuncia a primeira
que estoura, uma linha por handle. `[SETEVENT]` (sob `RECOMP_EVENT_TRACE`)
mostra as primeiras sinalizacoes por handle, com thread e call site.

```
[SETEVENT] tid 2804  sets 0x490 from 0x001D189D (#1..#3)
[SETEVENT] tid 15780 sets 0x494 from 0x001D189D (#1..#3)
[WAITLONG] tid 15780 blocked >2000 ms on handle 0x490 (INFINITE)
```

Um handoff ping-pong entre duas threads, os dois lados sinalizando do mesmo
helper `0x001D189D`. A consumidora (15780) trava esperando `0x490`, que a
produtora (2804) deveria sinalizar.

E a produtora e a **thread principal de render**: host `0x...4724D90` menos a
base do modulo da `0x141204D90` = `sub_00351DD0+0x190`, queimando 100% de CPU
no caminho de espera da GPU. Ela nao esta travada -- faz 120 fps -- mas nunca
chega ao ponto que sinalizaria `0x490`.

### Por que isso e um acordar perdido, e nao so "a outra parou"

`NtCreateEvent ... type=1` e **SynchronizationEvent**, ou seja auto-reset, nao
manual-reset. Num auto-reset:

- cada `NtSetEvent` libera **exatamente um** waiter;
- sinalizar duas vezes antes de alguem esperar **nao acumula**: o evento fica
  sinalizado uma vez e a segunda contagem se perde.

Um handoff que depende de contagem perde um acordar nesse caso e trava as duas
pontas para sempre. A barra parando exatamente num ponto e consistente com
isso: as tres primeiras rodadas do ping-pong passam, e uma delas perde.

### Proximo passo

Contar, nao amostrar (de novo): quantas vezes `0x490` e sinalizado contra
quantas vezes alguem espera por ele, e se o total de sets fica um abaixo do
total de waits. Se ficar, o defeito esta na nossa semantica de evento ou na
ordem em que o nosso `NtReadFile` sinaliza, nao na logica do titulo.

Vale conferir tambem `xbox_NtReadFile`: ele so chama `SetEvent(Event)` no
caminho de sucesso. Em `STATUS_END_OF_FILE` e em falha ele retorna **sem**
sinalizar, entao qualquer chamador que espere naquele evento fica parado para
sempre. Nao e o que trava aqui agora (o EOF de partition0 foi eliminado e o
travamento continua), mas e um acordar perdido garantido esperando acontecer.

## O livro-caixa de eventos inocenta a nossa sincronizacao (14/out)

`[EVTLEDGER]` conta sets e waits por handle, somando as tres portas de espera
(`NtWaitForSingleObjectEx`, `NtWaitForMultipleObjectsEx`, `KeWaitForSingleObject`),
e despeja o estado quando `[WAITLONG]` dispara:

```
[WAITLONG]  tid 3516 blocked >2000 ms on handle 0x490 (INFINITE)
[EVTLEDGER] 0x0035F7FC=0 sets/1 waits  0x490=41 sets/42 waits  0x494=41 sets/0 waits
```

**41 sets contra 42 waits.** Se a nossa implementacao tivesse engolido um
sinal -- a hipotese do auto-reset que nao acumula -- veriamos 42 sets e 42
waits com a consumidora parada mesmo assim. Vemos 41. A produtora **nunca
postou o trabalho numero 42**. As nossas primitivas de evento estao
inocentadas por contagem, e a causa esta na logica acima.

`0x494 = 41 sets / 0 waits` tambem desfaz a leitura anterior: o handoff nao e
um ping-pong simetrico. Um lado espera, o outro so sinaliza e segue.

### O que e esse handoff

Varrendo a pilha do guest no bridge de `NtSetEvent` (o call site registrado e
sempre o wrapper `SetEvent` do titulo, que nomeia ninguem -- 55 lugares o
chamam):

```
produtora  tid 15932 -> 0x490  <- 0x001B3A1F <- 0x001B378D <- 0x00011226
consumidora tid 3516 -> 0x494  <- 0x001B401B <- 0x001B4055 <- 0x001D1C4A
```

- `sub_001B3EE0` (`game_engine`) e o laco da worker: sinaliza, trabalha,
  espera, sinaliza. A sua entrada `sub_001B401E+0x22` = `0x001B4040` e
  exatamente o `ctx1` do `PsCreateSystemThreadEx #2` no log.
- `sub_001B39D0` e `sub_001B3A80` (`game_engine`) sao o lado de postar: ambos
  sinalizam sob condicao, a forma de "acorde a worker se a fila estava vazia".

E a fila de trabalhos do motor. A worker esta parada esperando o trabalho 42,
e a thread principal -- viva, a 120 fps -- parou de postar.

### Onde isso deixa a investigacao

Eliminados por medicao ate aqui: PFB flush, portoes de DMA, fence, o sleep de
10 Hz, o EOF de `partition0`, a renderizacao (a tela sai perfeita) e agora a
semantica de eventos. O que resta e a maquina de estados de loading da thread
principal: ela decide nao postar mais trabalho, e e preciso descobrir o que
ela consulta para decidir isso.

Correcao de um erro meu: eu disse num turno anterior que `type=1` em
`NtCreateEvent` seria manual-reset. E auto-reset (SynchronizationEvent). A
contagem tornou a distincao irrelevante para este travamento, mas o registro
fica certo.

## O laco que prende: `main()` e a maquina de estados de loading

`sub_00156740` (`game_engine`) e o `main()` do titulo -- a cadeia da thread
principal resolve ate `sub_001D2B53`, que e o `ctx1` do
`PsCreateSystemThreadEx #1`. Ele contem o laco exato:

```c
loc_001567A0:
    sub_0002F750();                     // pump
    sub_001D97E0(MEM32(0x4D6898));      // present -- a cadeia que o watchdog pega
    if (LO8(sub_00015FD0()) == 0)       // "ja terminou?"
        goto loc_001567A0;              // enquanto nao, repete
```

`sub_00015FD0` (`game_vtable`, 1678 bytes) e uma maquina de estados com jump
table de 14 alvos, indexada por `MEM32(<obj> + 0x2E1E8) - 1`, limitada a
0x17. Cada caso chama um sub-passo e, se ele devolver 0, salta para
`loc_0001635C` e o estado fica onde esta.

Entao: a thread principal apresenta a tela de loading a 120 fps e pergunta
"terminou?" todo frame; a resposta e sempre nao; e a worker esta bloqueada
esperando um trabalho que nunca e postado. As duas pontas esperam.

O objeto de loading (`0x003F9410`, o `ctx2` da worker) nao muda um unico
dword pelo resto da execucao, medido com `RECOMP_WATCH`.

### O que falta, em ordem

1. Identificar com certeza o objeto que `sub_00015FD0` recebe -- `0x4D6898`
   e o que `main()` passa para o present, nao necessariamente o mesmo -- e ler
   `+0x2E1E8` para saber **em qual estado** parou.
2. Ler o caso daquele estado e o sub-passo que devolve 0 para sempre.
3. So entao decidir o conserto, que ate aqui nao tem nada a ver com GPU nem
   com sincronizacao: as duas ja foram inocentadas por medicao.

## A cadeia completa do travamento, medida ponta a ponta (14/out)

O XBE nao abre no REA, mas um PE32 minimo com as mesmas VAs abre
(`tools` -> `xbe2pe.py` no scratchpad): mesma base 0x00010000, mesmas secoes
que o log de boot. O REA estourou o tempo em `sub_00015FD0` (1678 bytes num
binario de 4 MB), entao a cadeia saiu de `RECOMP_WATCH` + leitura do C.

Objeto da tela de loading: **`0x004A7560`**, empilhado explicitamente pelo
`main()` a cada iteracao. (Eu primeiro li `MEM32(0x4D6898)`, que e o argumento
do *present* -- erro de ter filtrado o `esp += 0xC` que equilibra a pilha.)

```
0x004D5748  estado de loading        0 -> 1 -> ... -> 10, para
   estado 10 -> loc_000162E3 -> sub_000FC9D0(0x572D30, 1)
0x005A37B4  sub-estado               0 -> 1, para
   sub-estado 1 -> loc_000FCA64 -> sub_001B6280(0x0057BFD8)
0x0057C058  estado interno           0 -> 1 -> 2, para
   estado 2 -> loc_001B634E: status = [0x0057C074]+0x17A8
0x...+17A8  status do pipeline       1 -> 2, para   (precisa chegar a 4)
```

A tabela de salto e o byte-map saem direto do binario (`MEM8(0x16698+i)` e
`MEM32(0x16660+idx*4)`), entao o mapeamento estado->caso e fato, nao leitura.

`sub_00210DA0`, chamada quando o status nao e 4, monta um registro com os
campos `USRPTR`, `NUMUSR`, `VOCBAN` -- usuario e voz. E `world/pipe/p2/xbox/
xbpipe.c`.

### Quem deveria avancar o status

`sub_00210B60` (mesma familia `xbpipe.c`) e quem escreve `+0x17A8`, com 11
referencias aos campos. Ela tem **zero chamadores diretos**: so e alcancavel
pela tabela de despacho indireto, ou seja por callback.

E o log mostra `KeConnectInterrupt` nos vetores 0, 1, 3, 5 e 6. A entrada da
`NV2A_ACK` para `PGRAPH_INTR` neste repositorio diz, com todas as letras:
*"nothing here ever raises a GPU interrupt, so 'none pending' is the truth"*.
Se esse callback pende de uma interrupcao que nos nunca levantamos, o status
nunca sai de 2 -- e nada acima dele pode avancar.

### Contorno tentado e descartado

`RECOMP_WAIT_KICK=<ms>` sinaliza um evento cuja espera ja ficou parada por
<ms>, que e o remedio padrao para acordar perdido. **553 cutucadas em 30 s e o
estado nao saiu de 10.** Acordar a worker nao adianta quando nao ha trabalho
pendente: a hipotese de acordar perdido esta morta. O mecanismo fica no codigo
porque e correto para a classe que cobre e e inerte desligado, com o resultado
negativo escrito no comentario para ninguem repetir.

### Proximo passo

Confirmar se `sub_00210B60` roda alguma vez. Se nao rodar, descobrir qual
vetor a dispara e levantar essa interrupcao -- o que e trabalho no nosso lado,
nao no titulo.

## Descendo mais: o callback roda, o relogio anda, a operacao nao completa

Tres hipoteses caidas por medicao neste turno, todas plausiveis e todas
erradas:

1. **Acordar perdido na fila de trabalhos.** `RECOMP_WAIT_KICK=50`: 553
   cutucadas em 30 s, estado nao saiu de 10.
2. **Callback nunca invocado.** `sub_00210B60` so e alcancavel por ponteiro, e
   o unico lugar que a registra e `sub_00210CF0` via `sub_00215CC0` numa
   tabela em `0x0041D118`. Mas ela **roda**: foi ela que escreveu o status 2.
3. **Relogio congelado.** A bomba de callbacks `sub_00215D60` tem um portao
   temporal: so percorre a tabela quando `agora() >= [0x0041D1B8]`, e repoe o
   portao em `agora() + 5`. Se o relogio parasse, a tabela nunca seria
   percorrida -- o congelamento exato que se via. Medido: o relogio
   (`*[0x0036BC60]`, um data export do kernel) avanca ~1000/s e o portao
   acompanha. A bomba roda normalmente.

### Onde para, de verdade

Com o callback rodando a cada tick, em status 2 ele chama
`sub_00215650(ctx, obj, 0x10)` e trata o retorno:

```c
if (r == A) { obj->17AC = 3; obj->17B0 = 8; obj->17A4 = 0; }  // erro
if (r == B) { obj->17AC = 1; }                                // pendente
if (r == C) { obj->17A8 = 3; }                                // avanca
```

Medido `+0x17AC == 1`: **`sub_00215650` devolve "pendente" toda vez**.

Ela le um gerenciador em `MEM32(0x0041D114)` = `0x00FFEA60` e chama
`sub_0036412F` (secao XPP), que embrulha o trabalho entre
`KeRaiseIrqlToDpcLevel` e `KfLowerIrql` -- os ordinais 129 e 161 crescem sem
parar no log, que e exatamente esse laco. O resultado da chamada e guardado em
`[0x00FFEA60]+8`, que foi de `FFFFFFFF` para **0** e congelou.

Nao e DPC: o ordinal 119 (`KeInsertQueueDpc`) nunca e chamado na execucao
inteira, e nao ha nenhum `[IRQLBUG]`. O IRQL ali e so um lock em volta de uma
fila consultada de forma sincrona.

### Estado

A cadeia esta completa de `main()` ate um unico valor: o resultado da chamada
XPP em `[0x0041D114]+8`, que e 0 e deveria virar um recurso valido. O proximo
passo e `sub_0036412F` e os dois thunks de kernel que ela chama por
`MEM32(0x36BC54)` e `MEM32(0x36BC58)` -- descobrir quais ordinais sao e o que
devolvem.

### Ferramenta

`tools/xbe/xbe2pe.py` converte o XBE num PE32 com as mesmas VAs, para
ferramentas que nao leem XBE. O REA carrega, mas estourou 5 min decompilando
uma funcao de 1678 bytes neste binario de 4 MB -- util para funcoes pequenas,
nao para as grandes.

## RESOLVIDO: a tela de loading esperava o Xbox Live (14/out)

O jogo passa da tela de loading. Carrega `Data/stagehed.bin`,
`tracks/US/C5_V1/enviro.dat` e `static.dat`, `_EATrax0.xwb`, `sound/fe.awd`,
`ovid/movie.xwb`, `ovid/englis30.xmv` e `cri_rw30.xmv`, e mostra o logo da
Criterion Games animando.

### A causa

A etapa 10 da maquina de loading espera um item numa lista em `0x00363088`.
Varrendo o binario inteiro atras de quem a alimenta -- decodificando cada
`e8 rel32` das secoes de codigo -- ha exatamente tres sitios de insercao:

```
.text    0x0024E26C   push 0x00362FF4   <- OUTRA lista
.text    0x0024E27B   push 0x00363000   <- OUTRA lista
XONLINE  0x0031BDA9   push 0x00363088   <- esta, e so ela
```

O unico produtor esta na secao XONLINE. Medido: a cabeca da lista fica em 0 a
execucao inteira. Offline esse produtor nunca roda, e o consumidor espera para
sempre. Bate com `sub_0031BCA4` devolver `0x80150005` (faixa de erro do Xbox
Live) e com os campos `USRPTR`/`NUMUSR`/`VOCBAN` do registro de diagnostico.

### O conserto

O proprio titulo previu a saida: `sub_00210CF0` arma um prazo de 15 s em
`+0x17BC`. Mas `sub_00210B60` so confere esse prazo enquanto `+0x17A4` vale 1;
quando passa a 2 -- que e onde travamos -- nao ha mais nenhuma verificacao de
tempo. **O timeout existe e nao cobre o estado em que o jogo fica preso.**

O contorno (`recomp_manual.c`, o lugar projetado para overrides a mao) engancha
`sub_00210B60`, que so e alcancavel por ponteiro, e honra o prazo que o titulo
armou: vencido ele, avanca o status de 2 para 3 -- exatamente a transicao que
`sub_00210B60` faria se `sub_00215650` tivesse respondido. Nao inventa dado
nenhum e nao tem numero para ajustar; o log confirma 15016 ms, o proprio prazo
do titulo. `RECOMP_SKIP_LIVE_WAIT=0` desliga.

Duas armadilhas no caminho, as duas medidas e nao supostas:

- A bomba de callbacks passa o contexto em `eax`, nao na pilha. Ler `g_esp+4`
  pegava lixo e o gancho nunca disparava, sem erro nenhum -- so silencio. O
  objeto e lido do global `0x0057C074`.
- `recomp_lookup_manual` so intercepta chamadas **indiretas**. `sub_00215650`,
  que seria o alvo mais obvio, e chamada direto e nao daria para enganchar.

### Metodo

Quatro hipoteses caidas antes desta, todas por medicao e nao por argumento:
acordar perdido na fila de trabalhos (553 cutucadas, nada), callback nunca
invocado (ele roda), relogio congelado (anda, ~1000/s), DPC nao drenado
(ordinal 119 nunca e chamado). Cada queda custou minutos porque o ciclo de
build/instalar/rodar/ler e local.

## O que o xrevive/Xrecomp ensina, e a causa raiz que ele aponta (14/out)

O usuario baixou o instalador do xrevive. Ele e NSIS: o payload e um stream
LZMA solido logo apos o `firstheader` (props `5d 00 00 80 00`, lc=3 lp=0 pb=2,
dicionario de 8 MB), 17 MB descomprimidos, de onde se extrai um app
Rust/Tauri de 16,9 MB.

Os repositorios nao ajudam -- **Xrecomp esta vazio** e o do xrevive tem so o
README --, mas o binario do launcher diz muita coisa.

### Convencao de linha de comando dos jogos

```
--xbe   --game-dir   --save-name
```

Eles parametrizam o que nos cravamos em `main.c` (`BO3_XBE_DEFAULT`,
`BO3_GAME_DIR`, `BO3_SAVE_DIR`). Ganho barato de usabilidade, sobretudo porque
testamos em outra maquina.

### A pista que importa: identidade de console

O launcher tem uma struct `ConsoleIdentity` de tres campos, com exemplo
embutido:

```json
{ "serial": "104920312005", "mac": "00:50:F2:1A:2B:3C", "hddKeyHint": "..." }
```

E a mensagem de erro dele e explicita:

> "No readable XB Online console identity yet. Launch an XBL-enabled game once
> to create it."

Ou seja: **o runtime recompilado deles gera uma identidade de console** --
serial, MAC e chave de HD, que num Xbox real vivem na EEPROM. A autenticacao
sai por `https://auth.insigniastats.live/api/`, entao o xb.live se apoia na
infraestrutura do Insignia.

### Ligando com o nosso travamento

Com `XBOX_LOG_LEVEL=5` (e numero, nao nome; a saida vai para
`xbox_kernel.log`, nao para stderr), o Burnout 3 pede estes indices de EEPROM:

```
tratados:      0x07 0x08 0x09 0x0A 0x11
NAO tratados:  0x101 (x1)  0x103 (x2)  0x104 (x2)  0xFFFF (x1)
```

O nosso `kernel.h` define ate `XC_MAX_OS = 0xFF`. **A faixa 0x100+ -- o bloco
de fabrica, onde moram numero de serie, endereco MAC e chave online -- nao
existe no nosso kernel**, e `0xFFFF` e a leitura da EEPROM inteira.

Isso e exatamente a mesma informacao que compoe o `ConsoleIdentity` deles. E
da uma explicacao de causa raiz para o travamento que eu contornei hoje por
timeout: a etapa de loading esperava um item que so o produtor em XONLINE
cria, e esse produtor nao consegue formar uma identidade de console porque
pedimos "unhandled" para serial, chave online e regiao -- e para a EEPROM
toda.

**Hipotese, nao fato comprovado**: responder coerentemente esses indices pode
fazer o XOnline concluir a inicializacao sozinho, tornando o gancho de timeout
desnecessario. O que e fato: o titulo pede, nos nao respondemos, e esses sao
os mesmos campos que o projeto concorrente precisa gerar.

### Plano

1. Implementar o bloco de fabrica em `ExQueryNonVolatileSetting` (serial, MAC,
   chave online, regiao AV, regiao de jogo) e `0xFFFF`, com valores sinteticos
   mas bem formados. Verificar a numeracao exata contra xemu/Cxbx antes, que o
   nosso header nao a define.
2. Rodar e ver se o pipeline sai do status 2 **sem** o gancho
   (`RECOMP_SKIP_LIVE_WAIT=0`). Se sair, a causa raiz esta resolvida e o
   contorno vira rede de seguranca.
3. Aceitar `--xbe/--game-dir/--save-name`.

## EEPROM: bloco de fabrica implementado, hipotese de causa raiz REFUTADA

### Correcao da anotacao anterior

A numeracao que escrevi estava errada. Conferida contra
`XboxDev/nxdk lib/xboxkrnl/xboxkrnl.h`:

| indice | eu havia escrito | real |
|---|---|---|
| 0x100 | -- | `XC_FACTORY_SERIAL_NUMBER` |
| 0x101 | serial | **`XC_FACTORY_ETHERNET_ADDR` (MAC)** |
| 0x102 | -- | `XC_FACTORY_ONLINE_KEY` |
| 0x103 | chave online | **`XC_FACTORY_AV_REGION`** |
| 0x104 | regiao AV | **`XC_FACTORY_GAME_REGION`** |
| 0xFFFF | EEPROM inteira | `XC_MAX_ALL`, correto |

O Burnout 3 pede MAC e as duas regioes -- nao pede serial nem chave online.
Entao a ligacao com o `ConsoleIdentity` do xrevive (serial/MAC/chave de HD) e
**mais fraca** do que eu afirmei: coincide so no MAC.

### O resultado: a hipotese nao se confirmou

Com os seis indices respondidos e **zero `unhandled`**, rodando com
`RECOMP_SKIP_LIVE_WAIT=0`, a maquina de loading **continua parando no estado
10**. Responder a EEPROM nao destrava o XOnline. O contorno de timeout
permanece necessario, e a causa raiz do travamento segue em aberto.

A implementacao fica porque e correta por si: um titulo que pergunta merece
resposta bem formada, e zeros com aviso nao sao resposta.

### Em troca, um bug latente de PAL

Derivar a regiao do disco e o certo, e foi o que implementei primeiro: o XBE
declara em `cert+0xA0` para que regiao foi prensado. Medido com o disco
europeu do usuario:

- regiao PAL (`0x04` / AV `0x300`): o titulo **trava no boot**, antes de abrir
  um unico arquivo.
- NTSC-M (`0x01` / AV `0x100`): boot normal, 37 arquivos, chega aos videos.

PAL-I e 50 Hz, com resolucao e temporizacao proprias, e o nosso caminho de AV
e NV2A nao as trata. O defeito e nosso, nao do valor. Entao o padrao fica
**deliberadamente NTSC-M mesmo para disco PAL**, com `RECOMP_REGION=pal` para
reproduzir o travamento, e `g_xbe_game_region` ja exposto para quando o
caminho PAL existir.

### Erro meu, removido

A primeira versao do `XC_MAX_ALL` escrevia a regiao no offset 0x58 do dump
cru, para o bloco concordar com 0x103/0x104. **Esse offset eu inventei** -- a
secao de fabrica real e cifrada e tem layout proprio, que eu nao verifiquei.
Fabricar estrutura nao conferida e pior que devolver zeros: zeros um titulo
reconhece como ausencia, um campo plausivel no lugar errado ele trata como
dado. Removido; devolve zeros.
