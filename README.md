# Meraki Switch Migrator

Aplicação local para migrar switches Cisco Meraki MS (inclusive stacks) entre
networks existentes da mesma organização. Ela não guarda a chave de API e exige
uma confirmação antes de cada corte.

![Exemplo da tela de planejamento de migração](docs/images/migration-plan-example.png)

> Mover um MS de network remove as configurações de portas e do switch. A
> aplicação reduz a interrupção ao preparar no destino as configurações globais
> selecionadas e reaplicar o snapshot logo após o *claim*.

## Uso rápido no Windows

Qualquer usuário pode clonar, baixar ou copiar este repositório para uma pasta
local. Com Python 3.11 ou superior instalado e disponível pelo comando
`python`, abra o PowerShell na pasta do projeto e execute:

```powershell
.\start.ps1
```

Na primeira execução, o script cria o ambiente isolado `.venv`, instala as
dependências necessárias e inicia a interface em `http://127.0.0.1:8765`.
Nas execuções seguintes, ele apenas verifica o ambiente e inicia a aplicação.
Também recria automaticamente o ambiente virtual caso ele esteja incompleto ou
tenha sido removido.

## Instalação manual

Requer Python 3.11 ou superior. Em PowerShell, na pasta do projeto:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[windows,dev]"
meraki-switch-migrator
```

Abra `http://127.0.0.1:8765`. A chave da Dashboard API precisa ter acesso de
leitura e configuração para networks, devices e switching da organização.

## Operação segura

- A chave fica somente na memória da sessão e é descartada após 30 minutos de
  inatividade, logout ou reinício.
- Os snapshots e o baseline do destino são protegidos com DPAPI no Windows.
- Networks associadas a Configuration Templates, organizações diferentes e
  membros parciais de um stack são bloqueados.
- Em falha, a fila para e tenta reverter todos os itens concluídos, em ordem
  inversa, além da configuração global do destino.
- Após o *claim* de um Catalyst, a Dashboard API pode levar alguns minutos para
  aceitar operações específicas do equipamento. A aplicação espera até 15
  minutos e consulta também o inventário da network de destino. Se a API ainda
  não tiver convergido, o lote fica **interrompido** — sem remover o equipamento
  novamente — e a tela oferece **Recuperar configuração pendente**. Essa ação
  somente reaplica e valida o snapshot no destino já confirmado.

Os grupos marcados na prévia substituem a configuração equivalente no destino
pela da origem. Grupos que a API não pode ler/reaplicar com segurança são
explicitamente bloqueados; nunca são omitidos silenciosamente.
