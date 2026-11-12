# agent-relay

**Junte as IAs de programação que você já paga num time só.** Quando uma acaba a cota, a próxima assume a cadeira dela sozinha, e nenhuma tarefa conta como pronta sem passar nos seus testes.

[English](README.md) · MIT · Python 3.10+ · sem dependências obrigatórias

## Pra que serve

Muita gente paga mais de uma ferramenta de IA pra programar: Claude Code, Cursor, Antigravity, Grok, Copilot e outras. Cada uma tem sua cota. Na prática você usa uma até bater o limite, copia o contexto na mão pra próxima, e enquanto isso as outras ficam paradas.

O agent-relay transforma essas assinaturas num **time com cadeiras** (construtor, designer, testador etc.):

- **Troca pela cota.** Cada cadeira tem uma lista de motores em ordem de preferência e sempre usa o primeiro que ainda tem cota. O Claude acabou? A cadeira de construtor passa pro Antigravity e continua a mesma tarefa. A cota do Claude renovou? A cadeira volta pra ele.
- **Um quadro de tarefas compartilhado.** A tarefa é da cadeira, não do produto, então não importa qual IA está sentada nela hoje. Quando mais de uma cadeira pode fazer a tarefa, pega quem tem mais cota sobrando.
- **Um portão de conclusão.** O "pronto" é recusado se o agente não diz o que mudou e como testou, se mexeu em pasta gerada (`dist/`) ou se o seu comando de teste falha. O motivo da recusa volta pro agente.
- **Previsão de cota, não só a porcentagem.** Os níveis vão de `normal` pra `economy` e `critical` olhando o ritmo de gasto ("vai acabar antes de renovar") e um orçamento diário pra limites semanais.
- **Revisor e planejador com IA (opcionais).** Um modelo barato lê cada tarefa concluída e devolve como `Redo` se ela não faz o que foi pedido. Quando o quadro esvazia, ele propõe algumas tarefas pequenas a partir do seu `GOALS.md`.
- **Extras opcionais:** correio entre os agentes pelo [MCP Agent Mail](https://github.com/Dicklesworthstone/mcp_agent_mail) e painel ao vivo no [Claude Code Agent Monitor](https://github.com/hoangsonww/Claude-Code-Agent-Monitor).

## Como os agentes recebem trabalho

| Tipo | Pra quem | Como funciona |
|---|---|---|
| `hook` | Claude Code, Grok CLI, Cursor | Quando o agente termina a vez, o gancho de parada roda `relay hook <cadeira>`. Se tem pedido, correio ou tarefa livre, ele continua em vez de parar. |
| `cli` | Antigravity CLI (`agy`), qualquer agente de terminal | O agent-relay abre o comando pra cadeira, com as instruções num arquivo. Fecha quando a cadeira troca de motor e reabre se ele fechar ou ficar mudo com trabalho esperando. |
| `window` | Apps de desktop sem gancho (Windows) | O despachante digita na caixa de conversa do app, só enquanto você não está digitando. Ele confere que a mensagem saiu mesmo e nunca aperta "enviar" duas vezes, porque em muitos apps esse botão vira "parar". |
| `manual` | Você ou qualquer outra coisa | Só aparece no quadro. |

## Começo rápido

```bash
pip install git+https://github.com/caionicfilho89-dot/agent-relay
cd seu-projeto
relay init            # cria o relay.json e põe .relay/ no .gitignore
# edite o relay.json: suas contas, cadeiras e comando de teste (veja examples/relay.json)
relay doctor          # confere comandos, pastas e o servidor de correio
relay hook-config builder   # mostra o trecho do gancho pro Claude Code / Cursor
relay add "Consertar o loop de redirecionamento do login" --for builder
relay run             # o despachante: cota, trocas, entregas, revisões
```

Os agentes usam a mesma linha de comando: `relay inbox`, `relay claim`, `relay status`, `relay done`. As instruções de cada cadeira já explicam isso pra eles.

## Modo automático vem desligado

Muitos agentes de terminal têm uma opção de "aprovar tudo". O agent-relay **só** acrescenta os `autonomous_args` de um motor quando você coloca `"allow_autonomous": true`, e o `relay doctor` avisa quando está ligado. Um agente que aprova tudo sozinho pode rodar qualquer comando que o seu usuário pode. Se ligar, use uma sandbox, um container ou uma cópia descartável do projeto, e deixe senhas e chaves fora do alcance.

## Cotas

Cada conta (`accounts`) tem uma fonte:

- `file`: você anota o uso com `relay quota set claude 5h 82` (por exemplo, o que aparece na tela de configurações do app);
- `command`: um script seu que imprime `{"windows": [{"name": "5h", "used_pct": 82, "resets_at": "..."}]}`;
- `grok_log`: lê o log do Grok CLI;
- `agy`: pergunta a cota ao Antigravity CLI;
- `none`: não mede.

Quando alguma ferramenta escreve "quota reached" ou "usage limit", a conta é marcada como esgotada por 24 h. Também dá pra marcar na mão com `relay quota exhausted <conta>`.

## Situação

Versão alfa. As ideias vêm de uma fábrica que rodou por semanas com seis agentes (Claude Code, Cursor, Antigravity, Grok CLI, um app do Grok e Gemini) construindo um projeto de verdade no Windows. Este pacote é essa fábrica reescrita pra ser configurável em vez de ter tudo fixo no código. Vai ter arestas: abra uma issue.

Licença MIT.
