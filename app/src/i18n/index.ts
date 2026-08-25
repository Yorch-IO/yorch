import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import en from "./en.json";
import es from "./es.json";

export const LANGUAGES = ["en", "es"] as const;
export type Language = (typeof LANGUAGES)[number];

const STORAGE_KEY = "company-brain.language";

function initialLanguage(): Language {
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved && (LANGUAGES as readonly string[]).includes(saved)) {
    return saved as Language;
  }
  // navigator.language is a full tag like "es-ES"; only the primary subtag
  // selects a bundle.
  const primary = navigator.language.split("-")[0];
  return primary === "es" ? "es" : "en";
}

export function setLanguage(language: Language): void {
  localStorage.setItem(STORAGE_KEY, language);
  void i18n.changeLanguage(language);
}

void i18n.use(initReactI18next).init({
  resources: { en: { translation: en }, es: { translation: es } },
  lng: initialLanguage(),
  fallbackLng: "en",
  interpolation: { escapeValue: false },
});

export default i18n;
