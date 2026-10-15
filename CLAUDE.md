# Diretrizes do projeto

Para quem chega agora -- pessoa ou agente. O que este projeto e, como se
trabalha nele, e as armadilhas que ja custaram tempo.

## O que e

Burnout 3 (Xbox) rodando nativamente em x86, por **recompilacao estatica**:
o XBE e traduzido para C, compilado, e ligado a um runtime que substitui o
kernel do Xbox, a GPU NV2A, a APU e o USB. Nao e emulacao de CPU.

O plano original de decompilacao manual esta em [SPEC.md](SPEC.md) e foi
pausado em favor do `xboxrecomp`. O estado real vive em
[`docs/findings/static_recomp_boot_debug.md`](docs/findings/static_recomp_boot_debug.md)
-- esse arquivo e a fonte de verdade sobre o que foi medido, o que caiu, e o
que continua em aberto. **Leia o fim dele antes de comecar qualquer coisa.**

## Onde fica o que

```
burnout3_decompiled/          este repo: notas, ferramentas de RE, SPEC
  docs/findings/              o diario tecnico (o arquivo acima)
  tools/                      parsing de XBE/XDVDFS, assinaturas, Ghidra
  vendor/xboxrecomp/          o runtime compartilhado (fork do usuario)
  vendor/burnout3/            o projeto do titulo (main.c, seeds, config)
```

`vendor/` nao e versionado aqui, exceto o ponteiro de commit de
`vendor/xboxrecomp`. Mudanca de runtime se commita **la**, e aqui entra uma
entrada de findings com o ponteiro atualizado.

No runtime:

| caminho | o que e |
|---|---|
| `src/kernel/` | kernel do Xbox em Win32, e o **executor de pushbuffer** (`nv2a_pb_exec.c`) com rasterizador de software |
| `src/d3d/` | camada D3D8 HLE (nao usada por este titulo) e o **backend de GPU** (`nv2a_gpu.c`, `nv2a_vsh_hlsl.c`) |
| `src/apu/` | APU MCPX: processador de voz, DSP (stub), saida XAudio2 |
| `src/video/` | janela GDI do framebuffer, player de FMV |
| `src/usb/` | OHCI e os controles |

O titulo linka D3D8LTCG e DirectSound **estaticamente**: ele escreve o
pushbuffer da NV2A e os registradores da APU direto. Nao ha chamada de API
para interceptar. Por isso a camada D3D8 HLE em `src/d3d/d3d8_*.c` nunca
executa neste jogo -- e por isso bugs moraram nela por toda a sua vida sem
ninguem notar.

## Como construir e rodar

Build cruzado de Linux para Windows:

```sh
cd vendor/burnout3/build-mingw && cmake --build . -j$(nproc)
# saida: vendor/burnout3/bin/burnout3.exe
```

Rodar a partir do WSL funciona (interop binfmt executa o .exe de verdade, com
GPU real). **Duas armadilhas, as duas ja custaram uma execucao cada:**

1. **Variavel de ambiente do Linux nao atravessa para o processo Windows.**
   E preciso listar em `WSLENV`:
   ```sh
   RECOMP_PB_GPU=1 WSLENV=RECOMP_PB_GPU ./bin/burnout3.exe
   ```
   Sem isso a flag simplesmente nao chega, e o sintoma e "a mudanca nao fez
   nada".
2. **O share do WSL diferencia caixa de letra; o disco do Xbox nao.** Ja
   tratado por `fix_path_case()` em `kernel_path.c`; se aparecer "There is a
   problem with the disc you are using", suspeite de abertura de arquivo
   falhando antes de suspeitar de qualquer outra coisa -- `xbox_kernel.log`
   diz qual.

### Flags que existem

| flag | para que |
|---|---|
| `RECOMP_PB_EXEC=1` | liga o executor de pushbuffer (sem ele, tela preta) |
| `RECOMP_PB_GPU=1` | desenha na GPU via D3D11 em vez do rasterizador de software |
| `RECOMP_PB_GPU_DUMP=1` | escreve o HLSL gerado em arquivo |
| `RECOMP_FLIP_HZ` / `RECOMP_NO_FLIP_PACE` | taxa de apresentacao, e desligar o pacing |
| `RECOMP_FB_DUMP=<prefixo>` | despeja o framebuffer em BMP |
| `RECOMP_APU_LEVEL=1` | taxas e picos de toda a cadeia de audio |
| `RECOMP_SKIP_LIVE_WAIT` | contorno do travamento esperando Xbox Live |

A lista completa esta espalhada pelos comentarios; `grep -rn getenv src/`
acha todas.

## Como se trabalha aqui

Isto nao e estilo pessoal, e o que faz o projeto avancar num binario sem
simbolos. Vale para agentes tambem.

1. **Medir, nao argumentar.** Toda afirmacao no findings vem com o numero que
   a sustenta. "A tela de disco sujo e regressao do backend grafico" virou
   falso em cinco minutos colocando as mudancas de lado e reconstruindo. Se
   voce nao mediu, escreva "hipotese" com todas as letras.
2. **Instrumento antes de conserto.** "Sem audio" tinha duas metades
   indistinguiveis da cadeira. Um medidor na ultima linha antes da placa de
   som (`RECOMP_APU_LEVEL`) separou as duas numa execucao. O instrumento fica
   no codigo depois.
3. **Toda mudanca de comportamento tem chave e contador.** Um caminho novo
   que nao pode ser desligado nao pode ser comparado, e um que nao conta o
   que recusou nao pode ser melhorado. O backend de GPU recusa batch por
   batch e cai no rasterizador de software, com o motivo contado.
4. **Hipotese derrubada tambem se registra.** O findings tem varias secoes
   que terminam em "a hipotese nao se confirmou". Isso impede a proxima
   pessoa de repetir o caminho.
5. **Um achado, um commit**, com a medicao na mensagem. No runtime a mensagem
   descreve a mudanca; aqui a entrada de findings descreve o que se aprendeu.
6. **Nao fabricar estrutura nao conferida.** Devolver zeros com aviso e
   melhor que inventar um campo plausivel no offset errado: zeros um titulo
   reconhece como ausencia, um valor plausivel ele trata como dado.

## Testes

Cada teste e um projeto CMake proprio em `vendor/xboxrecomp/tests/`, portavel,
sem GPU nem Windows:

```sh
cd vendor/xboxrecomp/tests/nv2a_vsh_hlsl
cmake -S . -B build && cmake --build build && ./build/nv2a-vsh-hlsl-test
```

Os que cobrem o nucleo grafico e de audio: `nv2a_vsh` (interpretador de
vertex program), `nv2a_vsh_hlsl` (o tradutor para HLSL), `nv2a_combiner`,
`apu_mixdown`, `audio_mixer`, `adpcm_decode`.

Do lado da ferramenta, em `vendor/xboxrecomp/tools/`, os testes sao pytest:

```sh
cd vendor/xboxrecomp && python3 -m pytest tools/disasm -q
```

`tools/disasm/test_seed_mid_instruction.py` fixa a politica de seeds que caem
dentro de uma instrucao -- ler o cabecalho dele antes de mexer em
`config/seed_functions.json`.

**Um teste novo tem de ser visto falhando.** O de `nv2a_vsh_hlsl` foi validado
reintroduzindo o bug historico (tabela de campos um dword fora) e conferindo
que quatro asserts quebram. Um teste que passou de primeira e nunca falhou
pode nao estar afirmando nada.

## Ler o codigo do titulo: o C levantado, nao o Ghidra

`vendor/burnout3/src/game/recomp/gen/` tem o titulo inteiro em C, lifted pelo
recompilador. Para entender o que o jogo faz, leia **de la**, nao do Ghidra:

- cobre exatamente o codigo que roda;
- nao custa dois minutos por funcao (o Ghidra estoura 120 s em funcoes grandes
  deste binario);
- e funciona para alvos que o Ghidra nao reconhece como funcao -- rotinas de
  DPC, callbacks e outros alvos que so existem por ponteiro, que sao
  justamente as interessantes quando se investiga um driver.

O REA continua util para a pergunta inversa: achar *quem* referencia um
endereco ou uma constante (foi assim que a unica referencia a `0xFED00000`
apareceu). Para constantes, um grep direto no XBE costuma ser mais rapido:
converter com `tools/xbe/xbe2pe.py` so e preciso para abrir no REA.

E para observar o estado em execucao ha `RECOMP_WATCH=<addr>[,<addr>]`, que
imprime cada mudanca com o `esp` de quem escreveu.

## Armadilhas ja pagas

- **`src/d3d/d3d8_vsh.c` decodifica microcodigo errado** (opcode MAC no bit 21
  da instrucao, quando o hardware o poe no bit 21 da palavra 1). Nao use. O
  tradutor correto e `src/d3d/nv2a_vsh_hlsl.c`, transliterado do
  interpretador. O gerador de combiners ao lado (`d3d8_combiners.c`) serve,
  mas tem tres divergencias corrigidas por substituicao de texto em
  `nv2a_gpu.c` -- leia o cabecalho de la antes de mexer.
- **A referencia de semantica e o nosso proprio interpretador**, nao o xemu:
  `nv2a_vsh_interp.c` e `nv2a_combiner.c` rodam todo frame e tem as regras que
  foram medidas (por exemplo: um ILU pareado com um MAC so pode escrever R1).
  Copiar codigo do xemu e decisao de licenca do dono do projeto -- ele e
  GPL/LGPL.
- **O titulo paceia a simulacao no contador de flips.** Qualquer coisa que
  mude a velocidade de apresentacao muda a velocidade do jogo; ver
  `flip_pace()`.
- **`nv2a_pgraph_d3d11.c` em `src/nv2a/` e esqueleto morto** para este
  titulo. O backend vivo e `src/d3d/nv2a_gpu.c`.
- **Um seed em `config/seed_functions.json` que cai dentro de uma instrucao
  nao adiciona uma funcao: ele trunca a que o contem, e em silencio.** Custou
  a investigacao inteira do USB -- um seed dois bytes dentro de um `cmp` cortou
  o construtor do controlador de 329 bytes para 25, e o sintoma foi "o jogo nao
  le o controle". O disasm agora nomeia cada um desses na saida (linhas `!!`);
  se uma funcao sair curta, suspeite deles primeiro. Regerar exige
  `./regen_linux.sh --disasm`: sem a flag o disasm e pulado e os limites vem do
  `functions.json` antigo.

## Prioridade atual

Fazer o jogo **rodar**: comandos, video e audio funcionais. Xbox Live,
redimensionamento de janela, resolucao interna e demais refinamentos ficam
para depois, mesmo quando a oportunidade aparecer no caminho.

Estado: video na GPU a 60 fps com pacing correto; audio localizado ate
`voice_resample()` -- as vozes estao ativas, roteadas e com envelope aberto, e
o PCM lido da memoria do guest e silencio.
