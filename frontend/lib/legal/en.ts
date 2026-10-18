import type { Legal } from "./index.ts";
import { ADDRESS, EMAIL, ENTITY, REGISTRATION, VAT } from "./owner.ts";

export const en: Legal = {
  terms: {
    title: "Terms of use",
    updated: "Last updated: 18 October 2026",
    sections: [
      {
        heading: "About these terms",
        body: [
          `ziftbook is an online booking service run by ${ENTITY} ("we", "us"). These terms apply when you book an appointment through ziftbook, and when you use a ziftbook account for your business.`,
          "How we handle personal data is explained in our privacy notice.",
        ],
      },
      {
        heading: "Booking an appointment",
        body: [
          [
            "Your appointment is an agreement between you and the business you book with. We provide the booking tool; we are not part of that agreement.",
            "The business decides its services, prices, opening times and its rules for cancelling and changing a booking. You see them before you book.",
            "Booking through ziftbook is free for you. You pay the business directly for its services.",
            "Some businesses confirm each booking by hand. Then your time is only booked once the business confirms, and we email you either way.",
            "You can change or cancel a booking with the link in your email, within the business's rules. For anything else about the appointment, contact the business.",
          ],
        ],
      },
      {
        heading: "Use ziftbook fairly",
        body: [
          "Please give your own, correct details, and only book times you mean to keep. Do not:",
          [
            "book with someone else's details without their permission;",
            "book or check times automatically, or copy content from ziftbook in bulk;",
            "try to get around our security checks or limits, or disrupt the service.",
          ],
          "We may refuse or remove bookings and accounts that break these rules.",
        ],
      },
      {
        heading: "Accounts for businesses",
        body: [
          [
            "You are responsible for your account and for the people you invite to your team. Keep your password to yourself.",
            "Keep the information about your business, your services, prices and booking rules correct.",
            "Your relationship with your clients is yours: you answer for your services, and for following the law, including the rules on sending offers and news.",
            "Prices for businesses, if any, are agreed with you separately.",
          ],
        ],
      },
      {
        heading: "Your clients' personal data",
        body: [
          "For the personal data of your clients (their names, contact details, bookings, notes, and the records kept with them), your business is the controller and we are your processor. These terms are our agreement for that processing. We:",
          [
            "process the data only to provide ziftbook to you, following your instructions as set out in these terms and in how you use ziftbook, including the transfers described in our privacy notice;",
            "make sure everyone who works on it is bound to keep it confidential;",
            "keep it secure, as described in our privacy notice;",
            "use only the service providers listed in our privacy notice, and tell you before we add or replace one, so you can object;",
            "make the same commitments binding on those providers, and remain responsible for them;",
            "tell you if we think an instruction breaks data protection law;",
            "help you answer your clients when they use their privacy rights, and with security, breach notifications and data protection impact assessments;",
            "tell you without undue delay if a personal data breach affects your clients' data;",
            "when you close your account, give you a copy of the data if you ask, then delete it, unless the law requires us to keep it;",
            "give you the information you need to show that this processing follows the law, and let you or an auditor you choose check it, with reasonable notice.",
          ],
        ],
      },
      {
        heading: "The service",
        body: [
          "We work to keep ziftbook available and safe, but we cannot promise it will always work without interruption or errors. We may change or improve features. We will tell businesses in advance about changes that affect how they use ziftbook.",
        ],
      },
      {
        heading: "Liability",
        body: [
          "We are not responsible for the appointment itself or for the business's services. As far as the law allows, we are not liable for indirect loss, such as lost income. Nothing in these terms limits liability that the law does not allow us to limit, and nothing in them affects your rights as a consumer.",
        ],
      },
      {
        heading: "Ending",
        body: [
          "You can stop using ziftbook at any time. A business can close its account by emailing us. We may suspend or close an account that breaks these terms, and we will tell you why.",
        ],
      },
      {
        heading: "Changes to these terms",
        body: [
          "When we change these terms, we update the date at the top. If a change matters for businesses, we email them before it takes effect.",
        ],
      },
      {
        heading: "Law and disputes",
        body: [
          "These terms are governed by Dutch law, and disputes go to the competent court in the Netherlands. If you are a consumer, you keep the protection of the mandatory law of the country where you live.",
        ],
      },
      {
        heading: "Contact",
        body: [`${ENTITY}, ${ADDRESS}. Registration number: ${REGISTRATION}. VAT number: ${VAT}. Email: ${EMAIL}.`],
      },
    ],
  },
  privacy: {
    title: "Privacy notice",
    updated: "Last updated: 18 October 2026",
    sections: [
      {
        heading: "Who we are",
        body: [
          `ziftbook is an online booking service run by ${ENTITY}, ${ADDRESS} (registration number ${REGISTRATION}). For any question about your personal data, email ${EMAIL}.`,
        ],
      },
      {
        heading: "Who decides what happens to your data",
        body: [
          [
            "When you book with a business through ziftbook, that business decides what happens with your details: it is the controller. We store and process them on its behalf, as its processor.",
            "For people with a ziftbook account (business owners and their teams), we are the controller of the account data.",
            "For the data we keep to protect the service (abuse checks and server logs), we are the controller.",
          ],
        ],
      },
      {
        heading: "When you book an appointment",
        body: [
          "We collect:",
          [
            "your name, email address and phone number;",
            "the booking: the service, the time, who it is with, the price, and whether it is confirmed, changed or cancelled;",
            "the language you used;",
            "your IP address and browser type, saved with each booking, change and cancellation as a record of who did what;",
            "if the business asks whether you want its offers or news: your answer, the exact wording you saw, the time, and your IP address and browser, as proof of your choice;",
            "notes the business keeps about you, if it adds any.",
          ],
          "We use this to make and manage your booking, to send you emails about it (such as a link to confirm your booking, a confirmation or the business's answer, a reminder about 24 hours before, and messages when it changes or is cancelled), and so the business can contact you about your appointment. The legal basis is the agreement you make with the business, the business's legitimate interest in keeping a record of bookings, our legitimate interest in preventing abuse, and your consent for offers and news, which you can withdraw at any time.",
          "In ziftbook, only the business you booked with (its owner and team) sees your details. Our staff access them only when needed to run or support the service. Every business has its own separate client list, and businesses cannot see each other's clients.",
          "If you are signed in to a ziftbook account when you book, we fill in your details from your account and link the booking to it.",
        ],
      },
      {
        heading: "If you have a ziftbook account",
        body: [
          "For business owners and team members we collect:",
          [
            "your email address, name and language, and your password, which we only store as a secure one-way hash;",
            "your business's name and country, your role, the name your clients see, your working hours and time off;",
            "the invitations you send, including the email addresses you invite;",
            "your sign-in sessions, with IP address and browser type;",
            "a record of changes made in your business's account: who did what and when, with IP address and browser type.",
          ],
          "We use this to provide your account, to keep it secure, and to show your business who changed what. The legal basis is our agreement with you and our legitimate interest in keeping the service secure.",
        ],
      },
      {
        heading: "Cookies",
        body: [
          "We only use cookies that ziftbook needs to work, never for advertising or analytics:",
          [
            "a sign-in cookie, when you sign in to a ziftbook account. It ends when you sign out, after 7 days without use, or after 30 days at most;",
            "a booking link cookie, when you open the link to your booking from an email. It lasts one hour;",
            "a staff sign-in cookie, for ziftbook's own administrators only. It lasts 8 hours at most.",
          ],
          "Your language is part of the web address, not a cookie. The team calendar remembers the view you chose in your browser. Cloudflare, which protects our site, may set its own security cookie.",
        ],
      },
      {
        heading: "Protecting against abuse",
        body: [
          "To stop automated and abusive bookings, the booking page uses Cloudflare Turnstile, which checks that a person is booking. For that check, your browser sends information such as your IP address to Cloudflare. We also count attempts per IP address and per email address, and store the email address for that count only as a one-way hash.",
        ],
      },
      {
        heading: "Emails",
        body: [
          "We send emails through Mailgun, from its EU region. Links in our emails are not tracked.",
        ],
      },
      {
        heading: "Service providers we use",
        body: [
          [
            "Hetzner Online: our servers, in Germany.",
            "Amazon Web Services: encrypted nightly backups of our database, in the EU.",
            "Cloudflare: the network in front of our site, and the Turnstile check. Cloudflare also stores the photos businesses add to their services, in the EU.",
            "Mailgun: sending emails, from its EU region.",
            "Grafana Labs: technical measurements, such as how long requests take, and our server logs, in the EU. These do not contain names, email addresses or phone numbers. Server logs are deleted after at most 14 days.",
          ],
          "Some of these companies are based in the United States. Where data may leave the EU, we rely on the safeguards the GDPR requires, such as the EU Standard Contractual Clauses or the EU-US Data Privacy Framework.",
        ],
      },
      {
        heading: "How long we keep data",
        body: [
          [
            "Bookings and client details: for as long as the business uses ziftbook, unless you or the business ask us to delete them sooner. When a business closes its account, we delete its data within 30 days.",
            "Account data: for as long as the account exists. We delete it within 30 days after the account is closed.",
            "Server logs: kept on our servers up to 7 days, at Grafana Labs up to 14 days. They are kept to what we need to run the service, mostly ids rather than names, and never the content of your messages.",
            "Backups: deleted automatically within two weeks.",
          ],
          "We keep something longer only when the law requires it.",
        ],
      },
      {
        heading: "Your rights",
        body: [
          "You can ask to see the personal data held about you, to correct it, to delete it, to limit its use, to object to its use, and to receive it in a format you can take elsewhere. Where you gave consent, you can withdraw it at any time.",
          `For a booking, the business you booked with handles your request, so contact it first. You can also email us at ${EMAIL}, and we will help or pass your request on.`,
          "If you think your data is mishandled, you can complain to the data protection authority in the EU country where you live or work.",
        ],
      },
      {
        heading: "Security",
        body: [
          "All connections to ziftbook are encrypted. Passwords are stored only as a secure hash. The database keeps each business's data separate, so one business cannot read another's. Backups are encrypted.",
        ],
      },
      {
        heading: "Changes to this notice",
        body: [
          "When we change this notice, we update the date at the top. If a change matters for account holders, we email them before it takes effect.",
        ],
      },
    ],
  },
};
