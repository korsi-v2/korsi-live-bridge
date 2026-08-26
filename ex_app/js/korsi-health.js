/*
 * SPDX-FileCopyrightText: 2026 Pishrun and Korsi contributors
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * The bridge's admin page: a self-test and a log tail.
 *
 * Hand-written, framework-free, no build step. This repository ships a Python container and has no
 * npm toolchain; adding Vue and a bundler to render two lists would mean a node_modules, a lockfile
 * and a build stage in the Dockerfile for a page an administrator opens when something is wrong.
 *
 * AppAPI renders the page itself -- a Nextcloud template containing only <div id="content"> -- and
 * rewrites this file's <script src> to go through its proxy. So everything below runs inside a normal
 * Nextcloud page, and every request goes to /apps/app_api/proxy/<appid>/..., where AppAPI attaches the
 * ExApp credentials. There is no token to manage here.
 */

(function () {
	'use strict'

	var APP_ID = 'korsi_live_bridge'
	var BASE = (window.OC && OC.generateUrl ? OC.generateUrl('/apps/app_api/proxy/' + APP_ID) : '/index.php/apps/app_api/proxy/' + APP_ID)

	/** Field names that must never be rendered, however the payload changes. Belt and braces: the
	 * bridge already redacts on the server, and this page is the one place a mistake there would be
	 * visible to a browser, a screenshot and a support ticket. */
	var REDACT = /secret|password|private|assertion|token$/i

	function request (path, options) {
		return fetch(BASE + path, Object.assign({
			headers: { Accept: 'application/json', requesttoken: (window.OC && OC.requestToken) || '' },
		}, options || {})).then(function (response) {
			if (!response.ok) {
				throw new Error('HTTP ' + response.status + ' from ' + path)
			}
			return response.json()
		})
	}

	function element (tag, attributes, children) {
		var node = document.createElement(tag)
		Object.keys(attributes || {}).forEach(function (name) {
			if (name === 'text') {
				node.textContent = attributes[name]
			} else if (name === 'class') {
				node.className = attributes[name]
			} else {
				node.setAttribute(name, attributes[name])
			}
		})
		;(children || []).forEach(function (child) {
			node.appendChild(child)
		})
		return node
	}

	/** The three states a check can be in. `null` is "not attempted", which is deliberately not
	 * failure: an unreachable Korsi and a Korsi nobody got as far as asking are different problems. */
	function verdict (ok) {
		if (ok === true) { return { symbol: '\u2713', label: 'passed', cls: 'korsi-ok' } }
		if (ok === false) { return { symbol: '\u2717', label: 'failed', cls: 'korsi-bad' } }
		return { symbol: '\u2013', label: 'not attempted', cls: 'korsi-skip' }
	}

	function section (title, body) {
		return element('section', { class: 'korsi-section' }, [
			element('h3', { text: title }),
			body,
		])
	}

	// ---------------------------------------------------------------- self-test

	function renderSelfTest (into, report) {
		var list = element('ul', { class: 'korsi-checks' })
		;(report.checks || []).forEach(function (check) {
			var state = verdict(check.ok)
			var item = element('li', { class: state.cls }, [
				element('span', { class: 'korsi-mark', 'aria-hidden': 'true', text: state.symbol }),
				element('div', { class: 'korsi-check-body' }, [
					element('strong', { text: check.name.replace(/_/g, ' ') }),
					element('span', { class: 'korsi-sr', text: ' ' + state.label + '. ' }),
					element('p', { text: check.detail || '' }),
				]),
			])
			if (check.rooms && check.rooms.length) {
				item.querySelector('.korsi-check-body').appendChild(
					element('p', { class: 'korsi-muted', text: 'Rooms: ' + check.rooms.join(', ') })
				)
			}
			if (check.roles && check.roles.length) {
				item.querySelector('.korsi-check-body').appendChild(
					element('p', { class: 'korsi-muted', text: 'Roles in the token: ' + check.roles.join(', ') })
				)
			}
			list.appendChild(item)
		})

		into.textContent = ''
		into.appendChild(list)
		if (report.service_key) {
			into.appendChild(renderServiceKey(report.service_key))
		}
	}

	/** The service key's shape, which is the failure this page exists for.
	 *
	 * A deployment platform that escapes backslashes in environment values turns the key's PEM into
	 * something with the right JSON fields, the right markers and no usable newlines. The bridge
	 * repairs that and still says so here, because a value repaired once needs repairing after every
	 * deploy and the operator can end it by switching to the base64 form. */
	function renderServiceKey (key) {
		if (!key.present) {
			return element('p', { class: 'korsi-bad-text', text: 'KORSI_SERVICE_KEY is not set.' })
		}
		var rows = [
			['Encoding', key.encoding === 'base64' ? 'base64 (recommended)' : 'raw JSON'],
			['Machine user', key.user_id || '\u2014'],
			['Key id', key.key_id || '\u2014'],
		]
		var table = element('table', { class: 'korsi-kv' })
		rows.forEach(function (row) {
			table.appendChild(element('tr', {}, [
				element('th', { scope: 'row', text: row[0] }),
				element('td', { text: String(row[1]) }),
			]))
		})

		var wrapper = element('div', { class: 'korsi-key' }, [
			element('h4', { text: 'Service key' }),
			table,
		])
		if (!key.usable) {
			wrapper.appendChild(element('p', { class: 'korsi-bad-text', text: key.problem || 'The key cannot be used.' }))
		}
		if (key.repairs && key.repairs.length) {
			wrapper.appendChild(element('p', {
				class: 'korsi-warn-text',
				text: 'This value arrived damaged and was repaired to make it usable (' + key.repairs.join('; ')
					+ '). Your deployment platform is escaping it. Ask Korsi for the base64 form of the key'
					+ ' and paste that instead \u2014 it contains no characters for a settings form to mangle.',
			}))
		}
		return wrapper
	}

	// ---------------------------------------------------------------- status

	function renderStatus (into, status) {
		var rows = [
			['Enabled in Nextcloud', status.enabled ? 'yes' : 'no'],
			['Watching for calls', status.watching ? 'yes' : 'no'],
			['Talk signaling configured', status.hpb_configured ? 'yes' : 'no'],
			['Version', status.version || '\u2014'],
			['Watched conversations', (status.rooms && status.rooms.length) ? status.rooms.join(', ') : 'none'],
		]
		var table = element('table', { class: 'korsi-kv' })
		rows.forEach(function (row) {
			table.appendChild(element('tr', {}, [
				element('th', { scope: 'row', text: row[0] }),
				element('td', { text: String(row[1]) }),
			]))
		})
		into.textContent = ''
		into.appendChild(table)

		if ((status.missing_configuration || []).length) {
			var list = element('ul', { class: 'korsi-problems' })
			status.missing_configuration.forEach(function (problem) {
				list.appendChild(element('li', { text: problem }))
			})
			into.appendChild(element('h4', { text: 'Configuration problems' }))
			into.appendChild(list)
		}
	}

	// ---------------------------------------------------------------- log

	function renderLog (into, payload) {
		into.textContent = ''
		if (!payload.available) {
			into.appendChild(element('p', { class: 'korsi-muted', text: 'No log file yet at ' + (payload.path || 'the configured path') + '.' }))
			return
		}
		if (!payload.records.length) {
			into.appendChild(element('p', { class: 'korsi-muted', text: 'Nothing logged at this level yet.' }))
			return
		}
		var log = element('ol', { class: 'korsi-log' })
		payload.records.forEach(function (record) {
			var extra = Object.keys(record)
				.filter(function (name) {
					return ['timestamp', 'level', 'logger', 'message', 'filename', 'function', 'line',
						'thread_name', 'pid', 'version', 'taskName'].indexOf(name) === -1 && !REDACT.test(name)
				})
				.map(function (name) { return name + '=' + JSON.stringify(record[name]) })
				.join(' ')

			log.appendChild(element('li', { class: 'korsi-level-' + String(record.level || '').toLowerCase() }, [
				element('time', { text: (record.timestamp || '').replace('T', ' ').slice(0, 19) }),
				element('span', { class: 'korsi-log-level', text: record.level || '' }),
				element('span', { class: 'korsi-log-message', text: record.message || '' }),
				element('span', { class: 'korsi-log-extra', text: extra }),
			]))
		})
		into.appendChild(log)
	}

	// ---------------------------------------------------------------- page

	function build (root) {
		root.textContent = ''
		var page = element('div', { class: 'korsi-health', id: 'korsi-health' })
		page.appendChild(element('h2', { text: 'Korsi live meeting bridge' }))
		page.appendChild(element('p', {
			class: 'korsi-lede',
			text: 'This bridge reads Talk calls in conversations Korsi has linked to a case and sends'
				+ ' their transcript to Korsi for live analysis. Nothing appears in Talk itself, so the'
				+ ' self-test below is how you tell a working bridge from a silent one.',
		}))

		var statusBody = element('div', { 'aria-live': 'polite' }, [element('p', { class: 'korsi-muted', text: 'Loading\u2026' })])
		var testBody = element('div', { 'aria-live': 'polite' }, [element('p', { class: 'korsi-muted', text: 'Running\u2026' })])
		var logBody = element('div', {}, [element('p', { class: 'korsi-muted', text: 'Loading\u2026' })])

		var rerun = element('button', { type: 'button', class: 'korsi-button', text: 'Run the self-test again' })
		var levelId = 'korsi-log-level-select'
		var level = element('select', { id: levelId, class: 'korsi-select' })
		;['INFO', 'WARNING', 'ERROR', 'DEBUG'].forEach(function (name) {
			level.appendChild(element('option', { value: name, text: name }))
		})
		var refreshLog = element('button', { type: 'button', class: 'korsi-button', text: 'Refresh the log' })

		function loadStatus () {
			return request('/api/v1/status')
				.then(function (status) { renderStatus(statusBody, status) })
				.catch(function (e) { fail(statusBody, e) })
		}
		function loadSelfTest () {
			testBody.textContent = ''
			testBody.appendChild(element('p', { class: 'korsi-muted', text: 'Running\u2026' }))
			rerun.disabled = true
			return request('/api/v1/selftest', { method: 'POST' })
				.then(function (report) { renderSelfTest(testBody, report) })
				.catch(function (e) { fail(testBody, e) })
				.then(function () { rerun.disabled = false })
		}
		function loadLog () {
			return request('/api/v1/logs?lines=200&min_level=' + encodeURIComponent(level.value))
				.then(function (payload) { renderLog(logBody, payload) })
				.catch(function (e) { fail(logBody, e) })
		}
		function fail (into, error) {
			into.textContent = ''
			into.appendChild(element('p', { class: 'korsi-bad-text', text: String(error.message || error) }))
		}

		rerun.addEventListener('click', loadSelfTest)
		refreshLog.addEventListener('click', loadLog)
		level.addEventListener('change', loadLog)

		page.appendChild(section('Self-test', element('div', {}, [
			element('p', {
				class: 'korsi-muted',
				text: 'Each step is the one the bridge itself performs, in order. The first failure is'
					+ ' the thing to fix; later steps are skipped when they would be meaningless.',
			}),
			testBody,
			rerun,
		])))
		page.appendChild(section('State', statusBody))
		page.appendChild(section('Recent log', element('div', {}, [
			element('div', { class: 'korsi-log-controls' }, [
				element('label', { for: levelId, text: 'Minimum level' }),
				level,
				refreshLog,
			]),
			logBody,
		])))

		root.appendChild(page)
		loadStatus()
		loadSelfTest()
		loadLog()
	}

	function start () {
		var root = document.getElementById('content')
		if (root) {
			build(root)
		}
	}

	if (document.readyState === 'loading') {
		document.addEventListener('DOMContentLoaded', start)
	} else {
		start()
	}
}())
