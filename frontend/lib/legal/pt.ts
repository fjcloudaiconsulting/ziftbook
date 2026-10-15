import type { Legal } from "./index.ts";
import { ADDRESS, EMAIL, ENTITY, REGISTRATION } from "./owner.ts";

export const pt: Legal = {
  terms: {
    title: "Termos de uso",
    updated: "Última atualização: [OWNER: data em que estes termos entram em vigor]",
    sections: [
      {
        heading: "Sobre estes termos",
        body: [
          `O ziftbook é um serviço de agendamento on-line operado por ${ENTITY} ("nós"). Estes termos valem quando você agenda um horário pelo ziftbook e quando você usa uma conta do ziftbook para o seu negócio.`,
          "Como tratamos dados pessoais está explicado no nosso aviso de privacidade.",
        ],
      },
      {
        heading: "Agendar um horário",
        body: [
          [
            "O seu horário é um acordo entre você e o estabelecimento onde você agenda. Nós fornecemos a ferramenta de agendamento; não fazemos parte desse acordo.",
            "O estabelecimento define seus serviços, preços, horários de funcionamento e suas regras para cancelar e alterar uma reserva. Você as vê antes de agendar.",
            "Agendar pelo ziftbook é gratuito para você. Você paga o estabelecimento diretamente pelos serviços.",
            "Alguns estabelecimentos confirmam cada reserva manualmente. Nesse caso, o seu horário só fica reservado quando o estabelecimento confirma, e enviamos um e-mail em qualquer caso.",
            "Você pode alterar ou cancelar uma reserva pelo link no seu e-mail, dentro das regras do estabelecimento. Para qualquer outro assunto sobre o atendimento, fale com o estabelecimento.",
          ],
        ],
      },
      {
        heading: "Use o ziftbook de forma justa",
        body: [
          "Informe os seus próprios dados, corretos, e só agende horários que você pretende cumprir. Não é permitido:",
          [
            "agendar com os dados de outra pessoa sem a permissão dela;",
            "agendar ou consultar horários de forma automática, ou copiar conteúdo do ziftbook em massa;",
            "tentar burlar nossas verificações de segurança ou limites, ou atrapalhar o serviço.",
          ],
          "Podemos recusar ou remover reservas e contas que descumpram estas regras.",
        ],
      },
      {
        heading: "Contas para negócios",
        body: [
          [
            "Você é responsável pela sua conta e pelas pessoas que convida para a sua equipe. Não compartilhe a sua senha.",
            "Mantenha corretas as informações sobre o seu negócio, seus serviços, preços e regras de reserva.",
            "A relação com os seus clientes é sua: você responde pelos seus serviços e pelo cumprimento da lei, inclusive das regras sobre o envio de ofertas e novidades.",
            "Eventuais preços para negócios são combinados com você separadamente.",
          ],
        ],
      },
      {
        heading: "Os dados pessoais dos seus clientes",
        body: [
          "Para os dados pessoais dos seus clientes (nomes, contatos, reservas e anotações), o seu negócio é o controlador e nós somos o seu operador. Estes termos são o nosso acordo sobre esse tratamento. Nós:",
          [
            "tratamos os dados apenas para fornecer o ziftbook a você, seguindo as suas instruções conforme estes termos e a forma como você usa o ziftbook;",
            "garantimos que todos que trabalham com eles têm dever de sigilo;",
            "mantemos os dados seguros, como descrito no nosso aviso de privacidade;",
            "usamos apenas os prestadores de serviço listados no nosso aviso de privacidade, e avisamos você antes de incluir ou substituir algum, para que você possa se opor;",
            "ajudamos você a responder aos seus clientes quando eles exercem seus direitos de privacidade, e em questões de segurança e proteção de dados;",
            "avisamos você sem demora injustificada se um incidente de segurança afetar os dados dos seus clientes;",
            "apagamos os dados quando você encerra a sua conta, a menos que a lei nos obrigue a mantê-los;",
            "fornecemos as informações de que você precisa para demonstrar que esse tratamento cumpre a lei.",
          ],
        ],
      },
      {
        heading: "O serviço",
        body: [
          "Trabalhamos para manter o ziftbook disponível e seguro, mas não podemos prometer que ele vai funcionar sempre sem interrupções ou erros. Podemos alterar ou melhorar funcionalidades. Avisamos os negócios com antecedência sobre mudanças que afetam a forma como usam o ziftbook.",
        ],
      },
      {
        heading: "Responsabilidade",
        body: [
          "Não somos responsáveis pelo atendimento em si nem pelos serviços do estabelecimento. Na medida em que a lei permite, não respondemos por danos indiretos, como lucros cessantes. Nada nestes termos limita uma responsabilidade que a lei não permite limitar, e nada neles afeta os seus direitos como consumidor.",
        ],
      },
      {
        heading: "Encerramento",
        body: [
          "Você pode parar de usar o ziftbook a qualquer momento. Um negócio pode encerrar a sua conta enviando um e-mail para nós. Podemos suspender ou encerrar uma conta que descumpra estes termos, e diremos o motivo.",
        ],
      },
      {
        heading: "Mudanças nestes termos",
        body: [
          "Quando mudamos estes termos, atualizamos a data no topo. Se uma mudança for importante para os negócios, enviamos um e-mail antes que ela entre em vigor.",
        ],
      },
      {
        heading: "Lei aplicável e conflitos",
        body: [
          "Estes termos são regidos por [OWNER: lei aplicável, por exemplo a lei holandesa], e os conflitos vão para [OWNER: foro competente]. Se você é consumidor, mantém a proteção das leis obrigatórias do país onde mora.",
        ],
      },
      {
        heading: "Contato",
        body: [`${ENTITY}, ${ADDRESS}. Número de registro: ${REGISTRATION}. E-mail: ${EMAIL}.`],
      },
    ],
  },
  privacy: {
    title: "Aviso de privacidade",
    updated: "Última atualização: [OWNER: data em que este aviso entra em vigor]",
    sections: [
      {
        heading: "Quem somos",
        body: [
          `O ziftbook é um serviço de agendamento on-line operado por ${ENTITY}, ${ADDRESS} (número de registro ${REGISTRATION}). Para qualquer dúvida sobre os seus dados pessoais, escreva para ${EMAIL}.`,
        ],
      },
      {
        heading: "Quem decide o que acontece com os seus dados",
        body: [
          [
            "Quando você agenda com um estabelecimento pelo ziftbook, é esse estabelecimento que decide o que acontece com os seus dados: ele é o controlador. Nós guardamos e tratamos os dados em nome dele, como operador.",
            "Para quem tem uma conta do ziftbook (donos de negócios e suas equipes), nós somos o controlador dos dados da conta.",
          ],
        ],
      },
      {
        heading: "Quando você agenda um horário",
        body: [
          "Coletamos:",
          [
            "o seu nome, e-mail e telefone;",
            "a reserva: o serviço, o horário, com quem, o preço, e se ela foi confirmada, alterada ou cancelada;",
            "o idioma que você usou;",
            "o seu endereço IP e o tipo de navegador, registrados a cada reserva, alteração e cancelamento, como registro de quem fez o quê;",
            "se o estabelecimento perguntar se você quer receber ofertas ou novidades: a sua resposta, o texto exato que você viu, a data e a hora, e o seu endereço IP e navegador, como prova da sua escolha;",
            "anotações que o estabelecimento faz sobre você, se fizer alguma.",
          ],
          "Usamos esses dados para criar e gerenciar a sua reserva, para enviar e-mails sobre ela (uma confirmação, um lembrete 24 horas antes, e mensagens quando ela for alterada ou cancelada), e para que o estabelecimento possa falar com você sobre o seu horário. A base legal é o acordo que você faz com o estabelecimento, o legítimo interesse dele em registrar as reservas e evitar abusos, e o seu consentimento para ofertas e novidades, que você pode retirar a qualquer momento.",
          "Só o estabelecimento onde você agendou vê os seus dados no ziftbook. Cada estabelecimento tem a sua própria lista de clientes, separada, e os estabelecimentos não veem os clientes uns dos outros.",
          "Se você estiver conectado a uma conta do ziftbook ao agendar, preenchemos os seus dados a partir da sua conta e vinculamos a reserva a ela.",
        ],
      },
      {
        heading: "Se você tem uma conta do ziftbook",
        body: [
          "De donos de negócios e membros de equipe, coletamos:",
          [
            "o seu e-mail, nome e idioma, e a sua senha, que guardamos apenas como um hash seguro de mão única;",
            "o nome e o país do seu negócio, a sua função, o nome que os seus clientes veem, o seu horário de trabalho e as suas folgas;",
            "os convites que você envia, com os e-mails que você convida;",
            "as suas sessões de acesso, com endereço IP e tipo de navegador;",
            "um registro das mudanças feitas na conta do seu negócio: quem fez o quê e quando, com endereço IP e tipo de navegador.",
          ],
          "Usamos esses dados para fornecer a sua conta, mantê-la segura e mostrar ao seu negócio quem mudou o quê. A base legal é o nosso acordo com você e o nosso legítimo interesse em manter o serviço seguro.",
        ],
      },
      {
        heading: "Cookies",
        body: [
          "Usamos apenas os cookies de que o ziftbook precisa para funcionar, nunca para publicidade ou análise:",
          [
            "um cookie de acesso, quando você entra em uma conta do ziftbook. Ele termina quando você sai, após 7 dias sem uso, ou após 30 dias no máximo;",
            "um cookie do link da reserva, quando você abre o link da sua reserva a partir de um e-mail. Ele dura uma hora.",
          ],
          "O seu idioma faz parte do endereço da página, não de um cookie. A agenda da equipe guarda no seu navegador a visualização que você escolheu. A Cloudflare, que protege o nosso site, pode definir um cookie de segurança próprio.",
        ],
      },
      {
        heading: "Proteção contra abusos",
        body: [
          "Para impedir reservas automáticas e abusivas, a página de agendamento usa o Cloudflare Turnstile, que verifica se é uma pessoa que está agendando. Para essa verificação, o seu navegador envia informações como o seu endereço IP para a Cloudflare. Também contamos as tentativas por endereço IP e por e-mail, e guardamos o e-mail para essa contagem apenas como um hash de mão única.",
        ],
      },
      {
        heading: "E-mails",
        body: [
          "Enviamos e-mails pela Mailgun, usando a região da UE. Os links nos nossos e-mails não são rastreados.",
        ],
      },
      {
        heading: "Prestadores de serviço que usamos",
        body: [
          [
            "Hetzner Online: os nossos servidores, na Alemanha.",
            "Amazon Web Services: backups noturnos criptografados do nosso banco de dados, na UE.",
            "Cloudflare: a rede na frente do nosso site, e a verificação Turnstile.",
            "Mailgun: o envio de e-mails, na região da UE.",
            "Grafana Labs: medições técnicas, como quanto tempo as requisições levam, na UE. Elas não contêm nomes, e-mails ou telefones.",
          ],
          "Algumas dessas empresas têm sede nos Estados Unidos. Quando os dados podem sair da UE, usamos as salvaguardas que o Regulamento Geral de Proteção de Dados da UE (GDPR) exige, como as Cláusulas Contratuais Padrão da UE ou o Quadro de Privacidade de Dados UE-EUA.",
        ],
      },
      {
        heading: "Por quanto tempo guardamos os dados",
        body: [
          [
            "Reservas e dados de clientes: enquanto o estabelecimento usar o ziftbook, a menos que você ou o estabelecimento peçam a exclusão antes. Quando um estabelecimento encerra a sua conta, apagamos os dados dele em até 30 dias.",
            "Dados da conta: enquanto a conta existir. Apagamos esses dados em até 30 dias após o encerramento da conta.",
            "Logs do servidor: até 7 dias. Eles registram o que aconteceu, com identificadores no lugar de nomes, e nunca o conteúdo das suas mensagens.",
            "Backups: apagados automaticamente em até duas semanas.",
          ],
          "Só guardamos algo por mais tempo quando a lei exige.",
        ],
      },
      {
        heading: "Os seus direitos",
        body: [
          "Você pode pedir para ver os dados pessoais que temos sobre você, corrigi-los, apagá-los, limitar o uso deles, se opor a esse uso, e recebê-los em um formato que possa levar para outro lugar. Quando você deu consentimento, pode retirá-lo a qualquer momento.",
          `Para uma reserva, quem cuida do seu pedido é o estabelecimento onde você agendou, então fale primeiro com ele. Você também pode escrever para ${EMAIL}, e nós ajudamos ou encaminhamos o seu pedido.`,
          "Se você achar que os seus dados estão sendo tratados de forma errada, pode reclamar à autoridade de proteção de dados do país da UE onde você mora ou trabalha.",
        ],
      },
      {
        heading: "Segurança",
        body: [
          "Todas as conexões com o ziftbook são criptografadas. As senhas são guardadas apenas como um hash seguro. O banco de dados mantém os dados de cada negócio separados, para que um negócio não possa ler os dados de outro. Os backups são criptografados.",
        ],
      },
      {
        heading: "Mudanças neste aviso",
        body: [
          "Quando mudamos este aviso, atualizamos a data no topo. Se uma mudança for importante para quem tem conta, enviamos um e-mail antes que ela entre em vigor.",
        ],
      },
    ],
  },
};
