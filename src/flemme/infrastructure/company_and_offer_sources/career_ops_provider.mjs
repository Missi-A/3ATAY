/* Exécute un connecteur public de Career Ops, sans démarrer ses outils de candidature. */
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const [root, providerId, portalUrl, fixturePath] = process.argv.slice(2);

try {
  if (!root || !portalUrl || !['greenhouse', 'lever', 'ashby'].includes(providerId)) {
    throw new Error('Dossier Career Ops, connecteur et URL du portail requis.');
  }
  const { name, version } = JSON.parse(await readFile(resolve(root, 'package.json'), 'utf8'));
  if (name !== 'career-ops' || version !== '1.40.0') {
    throw new Error('Ce pilote utilise la version officielle Career Ops 1.40.0.');
  }

  const load = (file) => import(pathToFileURL(resolve(root, file)).href);
  const [{ default: provider }, { makeHttpCtx }, { boardTruncation }] = await Promise.all([
    load(`providers/${providerId}.mjs`), load('providers/_http.mjs'), load('lib/board-truncation.mjs'),
  ]);
  const entry = { careers_url: portalUrl, name: new URL(portalUrl).pathname.split('/')[1] };
  const board = provider.detect(entry);
  if (!board) throw new Error('Portail non reconnu par Career Ops.');

  const fixture = fixturePath ? JSON.parse(await readFile(fixturePath, 'utf8')) : null;
  const endpoint = new URL(board.url);
  const published = new Map();
  let listRead = false;
  let missingLink = false;
  const http = makeHttpCtx();
  const offline = () => { throw new Error('Requête imprévue dans cet essai hors réseau.'); };
  const context = {
    ...http,
    ...(fixturePath ? { fetchText: offline, fetchResponse: offline } : {}),
    async fetchJson(url, options) {
      const request = new URL(url);
      const listing = request.origin === endpoint.origin && request.pathname === endpoint.pathname;
      if (fixturePath && !listing) offline();
      const data = fixturePath ? fixture : await http.fetchJson(url, options);
      if (!listing) return data;

      const jobs = providerId === 'lever' ? data : data?.jobs;
      if (!Array.isArray(jobs) || jobs.some((job) => !job || typeof job !== 'object' || Array.isArray(job))) {
        throw new Error('Format de la liste d’offres inattendu.');
      }
      listRead = true;
      const visible = jobs.filter((job) => job.isListed !== false);
      for (const job of visible) {
        const url = job.absolute_url || job.hostedUrl || job.jobUrl;
        const type = job.employmentType || job.categories?.commitment;
        if (typeof url !== 'string' || !url.trim()) missingLink = true;
        if (typeof url === 'string' && typeof type === 'string') published.set(url, type);
      }
      return providerId === 'lever' ? visible : { ...data, jobs: visible };
    },
  };

  const results = await provider.fetch(entry, context);
  if (!listRead) throw new Error('Career Ops n’a pas pu lire la liste du portail.');
  const truncation = boardTruncation(results) ?? (missingLink ? { reason: 'Lien absent du flux.' } : null);
  const jobs = results.map((job) => ({
    externalId: job.externalId,
    url: job.url,
    title: job.title,
    location: job.location,
    description: job.description,
    postedAt: job.postedAt,
    contractLabel: published.get(job.url),
  }));
  process.stdout.write(JSON.stringify({ provider: providerId, jobs, truncation }) + '\n');
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
