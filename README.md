# Burnout 3: Takedown — recompilação estática

Rodar o Burnout 3 do Xbox nativamente em x86, sem emular a CPU: o executável do
disco (`default.xbe`) é traduzido para C, compilado, e ligado a um runtime que
substitui o kernel do Xbox, a GPU NV2A, a APU e o USB.

Não é emulador e não contém nada do jogo. **Você precisa do seu próprio
disco.**

## Como está hoje

Funciona:

| | |
|---|---|
| vídeo | desenha na GPU via Direct3D 11; pacing em 59,94 Hz, medido **59,5–60 fps** |
| comandos | dois controles enumeram pelo OHCI, 16074 relatórios e 12 setups |
| áudio | oito vozes tocando, pico de 10828 de 32767 chegando à placa |

Não funciona ainda:

- **trava na tela de criação de perfil** — é o que impede de chamar de jogável;
- o caminho **PAL 50 Hz** trava no boot, então forçamos NTSC-M mesmo com disco
  europeu;
- os **vídeos de introdução são mudos**: o player decodifica só vídeo;
- o **DSP do MCPX** (GP/EP) é stub, então efeitos que dependem dele não soam.

Todo número acima foi medido, e a medição está no diário técnico (abaixo). Se
algo aqui não tiver número do lado, é porque ainda não foi medido — e aí está
escrito como hipótese.

## O que você precisa

- O seu disco do Burnout 3, extraído para uma pasta (`default.xbe`, `Data/`,
  `tracks/`, `sound/`, ...). O projeto foi medido com a versão europeia;
  `.recomp.json` traz o SHA-1 do `default.xbe` que usamos, para você conferir a
  sua cópia.
- Windows para rodar. O build é cruzado a partir de Linux com MinGW-w64.
- Python 3 e CMake para gerar e compilar.

## Clonar

O runtime entra como dois submódulos, `vendor/xboxrecomp` (kernel, NV2A, APU,
USB — compartilhado entre títulos) e `vendor/burnout3` (o projeto deste
título):

```sh
git clone --recurse-submodules git@github.com:Almirante-Ming/b3_decompiled
```

Se já clonou sem eles: `git submodule update --init --recursive`.

> **Hoje isso não reproduz o estado descrito acima, e a razão é simples:** os
> commits que fazem o jogo chegar a 60 fps ainda não foram publicados. São 29
> em `vendor/xboxrecomp` e 5 em `vendor/burnout3`, e eles existem apenas na
> máquina onde o trabalho foi feito. Os remotos configurados são os upstream
> (`sp00nznet/xboxrecomp` e `sp00nznet/burnout3`), onde ninguém daqui tem
> escrita, então os SHAs que este repositório grava não resolvem lá — um
> `submodule update` falha neles.
>
> Para resolver: dar fork dos dois no GitHub, empurrar as branches locais, e
> trocar as duas `url` do [`.gitmodules`](.gitmodules) para os forks. Enquanto
> isso não acontece, o repositório serve como notas, ferramentas e metadados;
> o que compila está na máquina de origem.

## Construir

Ponha o disco extraído em `vendor/burnout3/Burnout 3 Takedown/`, e então:

```sh
cd vendor/burnout3
./regen_linux.sh --disasm     # XBE -> C  (o --disasm importa, veja abaixo)
cd build-mingw && cmake --build . -j$(nproc)
```

A saída é `vendor/burnout3/bin/burnout3.exe`.

> **`--disasm` não é opcional quando você mexe em `config/seed_functions.json`.**
> Sem a flag o passo de desmontagem é pulado e os limites de função vêm do
> `functions.json` antigo — o build "funciona" e ignora a sua mudança.

## Rodar

No Windows, `Play (Keyboard).cmd` — ele já liga o que este título precisa, cada
chave com a medição que a justifica no comentário.

As chaves principais, se quiser rodar à mão:

| chave | para quê |
|---|---|
| `RECOMP_PB_EXEC=1` | executa o pushbuffer (sem ela, tela preta) |
| `RECOMP_PB_GPU=1` | desenha na GPU em vez do rasterizador de software |
| `RECOMP_USB=1` | sem ela o controle não enumera |
| `RECOMP_APU_PHYS=va` | faz as vozes de buffer soarem |

Para diagnosticar há mais, todas documentadas onde são lidas
(`grep -rn getenv vendor/xboxrecomp/src`): `RECOMP_APU_LEVEL`,
`RECOMP_USB_STATS`, `RECOMP_WATCH`, `RECOMP_HANG_WATCHDOG`, `RECOMP_FB_DUMP`,
`RECOMP_PAD_SCRIPT`.

Rodando pelo WSL há duas armadilhas, as duas já custaram uma execução cada, e
estão descritas no [CLAUDE.md](CLAUDE.md): variável de ambiente do Linux não
atravessa para o processo Windows (precisa de `WSLENV`), e o share do WSL
diferencia caixa de letra enquanto o disco do Xbox não.

## Onde fica o quê

```
.recomp.json        metadados do projeto (schema recomp.fyi)
CLAUDE.md           diretrizes: como se trabalha aqui, e as armadilhas já pagas
SPEC.md             o plano original, de decompilação manual — pausado, ver abaixo
docs/findings/      o diário técnico: o que foi medido, o que caiu, o que falta
tools/xbe/          leitura de XBE e extração de XDVDFS
tools/signatures/   FLIRT: casar funções com as libs do XDK e da RenderWare
tools/ghidra/       scripts de análise em lote
vendor/xboxrecomp/  runtime compartilhado (kernel, NV2A, APU, USB)  [submódulo]
vendor/burnout3/    o projeto do título (main.c, seeds, config)     [submódulo]
package.json        só os agentes do REA, usados para desmontar referências
```

**[`docs/findings/static_recomp_boot_debug.md`](docs/findings/static_recomp_boot_debug.md)
é a fonte de verdade** sobre o estado real. Leia o fim dele antes de começar
qualquer coisa: cada entrada tem a medição que a sustenta, e as hipóteses que
caíram estão registradas junto, para ninguém repetir o caminho.

O [SPEC.md](SPEC.md) descreve o plano inicial — decompilar função a função com
Ghidra e assinaturas. Esse plano foi **pausado** em favor da recompilação
estática com o `xboxrecomp`; o que sobrou dele de útil são as ferramentas em
`tools/` e o levantamento de bibliotecas do XDK.

## Testes

São 27 suítes em `vendor/xboxrecomp/tests/`, 25 delas um projeto CMake próprio,
portável, sem GPU nem Windows:

```sh
cd vendor/xboxrecomp/tests/nv2a_vsh_hlsl
cmake -S . -B build && cmake --build build && ./build/nv2a-vsh-hlsl-test
```

E os do desmontador, com pytest (precisa de `capstone` e `pytest` no ambiente
— 58 testes, ~0,5 s):

```sh
cd vendor/xboxrecomp && python3 -m pytest tools/disasm -q
```

Um teste novo aqui tem de ser **visto falhando** antes de entrar. O de
`nv2a_vsh_hlsl` foi validado reintroduzindo o bug histórico que ele cobre, e o
de `test_seed_mid_instruction.py` usa os bytes reais da instrução que o seed
ruim partia.

## Legal

O código do jogo e os assets são da Criterion/EA. Este repositório não contém
nenhum deles: só ferramentas, notas e a cola que liga o binário que **você**
possui a um runtime nativo. Nada aqui ajuda a obter o jogo.
