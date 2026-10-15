// Facts only the owner can give, used by every language. tests/legal.test.mjs fails while any
// "[OWNER:" marker remains in these pages, so they cannot ship unfilled (ZIF-147).
export const ENTITY = "[OWNER: legal entity name]";
export const REGISTRATION = "[OWNER: company registration number, e.g. KvK 12345678]";
export const ADDRESS = "[OWNER: postal address]";
export const EMAIL = "[OWNER: contact email for privacy and legal questions]";
