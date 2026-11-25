from __future__ import annotations

import dns.resolver
import logging
import requests
import time

from acme import client, errors, messages
from acme import challenges
from acme.client import ClientV2
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtensionOID, NameOID
from datetime import datetime, UTC
from josepy import JWKRSA

from mimic.aws import create_dns_record, delete_dns_record, normalize_domain
from mimic.settings import data_dir, ensure_private_dir, restrict_permissions
from mimic.tls import write_private_key

log = logging.getLogger(__name__)

LE_PROD = "https://acme-v02.api.letsencrypt.org/directory"
LE_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"


def _get_account_key():
    cert_dir = ensure_private_dir(data_dir() / "certs")

    account_key = cert_dir / "account.key"

    if account_key.exists():
        restrict_permissions(account_key)
        return serialization.load_pem_private_key(
            account_key.read_bytes(),
            password=None,
        )

    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=4096,
    )

    write_private_key(account_key, key)

    return key


def _read_registration_uri():
    cert_dir = (data_dir() / "certs")
    registration_path = cert_dir / "account.registration.json"

    if not registration_path.exists():
        return None

    try:
        return registration_path.read_text().strip() or None
    except Exception as e:
        log.debug(f"Failed to read ACME registration URI: {e}")
        return None


def _write_registration_uri(uri: str):
    cert_dir = (data_dir() / "certs")
    cert_dir.mkdir(parents=True, exist_ok=True)
    registration_path = cert_dir / "account.registration.json"
    registration_path.write_text(uri)


def _build_registration_resource(uri: str):
    return messages.RegistrationResource(
        body=messages.Registration.from_data(),
        uri=uri,
    )


def _create_acme_client(directory_url: str):
    account_key = _get_account_key()
    acme_client = None

    try:
        jwk = JWKRSA(key=account_key)
        directory = messages.Directory.from_json(requests.get(directory_url, timeout=30).json())

        net = client.ClientNetwork(jwk)
        acme_client = ClientV2(directory, net)

        persisted_uri = _read_registration_uri()
        if persisted_uri:
            try:
                log.debug("Using persisted Let's Encrypt account registration")
                regr = acme_client.query_registration(_build_registration_resource(persisted_uri))
                if getattr(regr, "uri", None):
                    _write_registration_uri(regr.uri)
                return acme_client
            except Exception as e:
                log.debug(f"Failed to use persisted ACME registration: {e}")
                log.debug("Falling back to creating a new ACME account")

        log.debug("Trying to create a new Let's Encrypt account")
        regr = acme_client.new_account(messages.NewRegistration.from_data(terms_of_service_agreed=True))
        if getattr(regr, "uri", None):
            _write_registration_uri(regr.uri)

        return acme_client
    except errors.ConflictError as e:
        account_uri = getattr(e, "location", None)
        if not account_uri and getattr(e, "response", None) is not None:
            account_uri = e.response.headers.get("Location")

        if account_uri:
            log.debug("Account already exists, fetching the registration URI")
            _write_registration_uri(account_uri)
            try:
                regr = acme_client.query_registration(_build_registration_resource(account_uri))
                if getattr(regr, "uri", None):
                    _write_registration_uri(regr.uri)
                return acme_client
            except Exception as query_error:
                log.error(f"Failed to query existing ACME registration: {query_error}")

        log.error(f"Failed to create Acme Client: {e}")
        return None
    except Exception as e:
        log.error(f"Failed to create Acme Client: {e}")
        return None


def _generate_csr(domains):
    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=4096,
    )

    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name(
               [x509.NameAttribute(NameOID.COMMON_NAME, domains[0])]
           )
    ).add_extension(
        x509.SubjectAlternativeName(
            [x509.DNSName(d) for d in domains]
        ),
        critical=False,
    ).sign(key, hashes.SHA256()))

    return csr.public_bytes(serialization.Encoding.PEM), key


def _wait_for_dns_propagation(fqdn: str, value: str, timeout: int = 300):
    deadline = time.time() + timeout
    log.info("Waiting for DNS propagation")
    while time.time() < deadline:
        try:
            answers = dns.resolver.resolve(fqdn, "TXT")
            for record in answers:
                txt = "".join(
                    s.decode() if isinstance(s, bytes) else s
                    for s in record.strings
                )
                if txt == value:
                    log.debug("DNS has propagated")
                    return

        except Exception as e:
            log.debug(e)

        time.sleep(10)

    raise TimeoutError("DNS propagation timeout")


def _valid_cert_exists(domains):
    cert_dir = (data_dir() / "certs")
    if not cert_dir.exists():
        log.debug("No existing certificate found")
        return False

    privkey_path = cert_dir / "certificate.key"
    fullchain_path = cert_dir / "fullchain.pem"

    if privkey_path.exists() and fullchain_path.exists():
        log.debug("Found existing certificates")
        with open(fullchain_path, "rb") as fullchain, open(privkey_path, "rb") as privkey:
            cert = x509.load_pem_x509_certificate(fullchain.read())
            key = serialization.load_pem_private_key(privkey.read(), password=None)

            if cert.not_valid_after_utc < datetime.now(UTC):
                log.debug(f"Certificate expired {cert.not_valid_after_utc}")
                return False

            cert_domains = []
            try:
                san_ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
                san_names = san_ext.value.get_values_for_type(x509.DNSName)
                cert_domains.extend(san_names)
                log.debug(f"Domains in existing certificate: {cert_domains}")
            except Exception as e:
                log.error(f"Failed to validate existing certificate:\n{e}")
                return False

            for domain in domains:
                if domain not in cert_domains:
                    log.debug(f"Domain {domain} not covered by existing cert")
                    return False

            # Check if public key and private key match
            pem, spki = serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            cert_pub = cert.public_key().public_bytes(pem, spki)
            key_pub = key.public_key().public_bytes(pem, spki)

            return cert_pub == key_pub

    log.debug("No existing certificate found")
    return False


def generate_certificate(domains, staging=False):
    cert_dir = (data_dir() / "certs")
    privkey_path = cert_dir / "certificate.key"
    fullchain_path = cert_dir / "fullchain.pem"

    domains = [normalize_domain(x) for x in domains]
    if _valid_cert_exists(domains):
        log.debug("Found valid certificates")
        return True

    log.info("Generating certificates")
    directory_url = LE_STAGING if staging else LE_PROD
    log.debug(f"Using {directory_url}")
    acme_client = _create_acme_client(directory_url)
    if not acme_client:
        log.error("Failed to create ACME client")
        return False

    try:
        csr, cert_key = _generate_csr(domains)
    except Exception as e:
        log.error(f"Failed to generate CSR: {e}")
        return False

    log.debug(f"Generated CSR:\n{csr!r}")
    order = acme_client.new_order(csr_pem=csr)
    log.debug(f"Requested OrderResource:\n{order!r}")

    created_records = []

    try:
        for authz in order.authorizations:
            dns_challenge = None

            for challenge_body in authz.body.challenges:
                if isinstance(challenge_body.chall, challenges.DNS01):
                    dns_challenge = challenge_body
                    log.debug(f"DNS Challenge:\n {dns_challenge}")
                    break

            if not dns_challenge:
                log.error(f"No DNS challenge for {authz.body.identifier.value}")
                return False

            validation = dns_challenge.validation(acme_client.net.key)
            log.debug(f"DNS validation value: {validation!r}")

            fqdn = "_acme-challenge." + authz.body.identifier.value
            log.debug(f"DNS validation FQDN: {fqdn!r}")

            if not create_dns_record(fqdn, validation, "TXT"):
                log.error("Failed to create certificate validation DNS records")
                return False

            created_records.append((fqdn, validation))

            _wait_for_dns_propagation(fqdn, validation)
            acme_client.answer_challenge(
                dns_challenge,
                dns_challenge.response(acme_client.net.key),
            )

        finalized = acme_client.poll_and_finalize(order)
        cert_pem = finalized.fullchain_pem
        log.debug(f"Issued Certificate:\n{cert_pem!r}")

        write_private_key(privkey_path, cert_key)

        fullchain_path.write_text(cert_pem)
        log.info("Certificates created successfully")

    except Exception as e:
        log.error(f"Failed to generate certificates:\n{e}")
        return False

    finally:
        for fqdn, _ in created_records:
            delete_dns_record(fqdn)

    return True
