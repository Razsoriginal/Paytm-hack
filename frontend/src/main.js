import '@fontsource-variable/plus-jakarta-sans/wght.css';
import '@fontsource-variable/bricolage-grotesque/wght.css';
import '@fontsource-variable/noto-sans-devanagari/wght.css';
import './app.css';
import { mount } from 'svelte';
import App from './App.svelte';

mount(App, { target: document.getElementById('app') });
