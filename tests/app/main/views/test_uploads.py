import re
import urllib
from unittest.mock import Mock

import pytest
from flask import make_response, url_for
from freezegun import freeze_time
from requests import RequestException

from app.main.views.uploads import format_recipient
from app.utils import normalize_spaces
from tests.conftest import (
    SERVICE_ONE_ID,
    create_active_caseworking_user,
    create_active_user_with_permissions,
)


@pytest.mark.parametrize('extra_permissions', (
    [],
    ['letter'],
    ['upload_letters'],
    pytest.param(
        ['letter', 'upload_letters'],
        marks=pytest.mark.xfail(raises=AssertionError),
    ),
))
def test_no_upload_letters_button_without_permission(
    client_request,
    service_one,
    mock_get_uploads,
    mock_get_jobs,
    extra_permissions,
):
    service_one['permissions'] += extra_permissions
    page = client_request.get('main.uploads', service_id=SERVICE_ONE_ID)
    assert not page.find('a', text=re.compile('Upload a letter'))


@pytest.mark.parametrize('extra_permissions, expected_empty_message', (
    (['letter'], (
        'You have not uploaded any files yet. '
        'To upload a list of contact details, first choose a template.'
    )),
    (['letter', 'upload_letters'], (
        'You have not uploaded any files yet. '
        'Upload a letter and Notify will print, pack and post it for you. '
        'To upload a list of contact details, first choose a template.'
    )),
))
def test_get_upload_hub_with_no_uploads(
    mocker,
    client_request,
    service_one,
    mock_get_no_uploads,
    extra_permissions,
    expected_empty_message,
):
    mocker.patch('app.job_api_client.get_jobs', return_value={'data': []})
    service_one['permissions'] += extra_permissions
    page = client_request.get('main.uploads', service_id=SERVICE_ONE_ID)
    assert normalize_spaces(' '.join(
        paragraph.text for paragraph in page.select('main p')
    )) == expected_empty_message
    assert not page.select('.file-list-filename')


def test_get_upload_hub_page(
    mocker,
    client_request,
    service_one,
    mock_get_uploads,
):
    mocker.patch('app.job_api_client.get_jobs', return_value={'data': []})
    service_one['permissions'] += ['letter', 'upload_letters']
    page = client_request.get('main.uploads', service_id=SERVICE_ONE_ID)
    assert page.find('h1').text == 'Uploads'
    assert page.find('a', text=re.compile('Upload a letter')).attrs['href'] == url_for(
        'main.upload_letter', service_id=SERVICE_ONE_ID
    )

    uploads = page.select('tbody tr')

    assert normalize_spaces(uploads[0].text.strip()) == (
        'some.csv '
        'Sent 1 January 2016 at 11:09am '
        '0 sending 8 delivered 2 failed'
    )
    assert uploads[0].select_one('a.file-list-filename-large')['href'] == (
        '/services/{}/jobs/job_id_1'.format(SERVICE_ONE_ID)
    )

    assert normalize_spaces(uploads[1].text.strip()) == (
        'some.pdf '
        'Sent 1 January 2016 at 11:09am '
        'Firstname Lastname '
        '123 Example Street'
    )
    assert normalize_spaces(str(uploads[1].select_one('.govuk-body'))) == (
        '<p class="govuk-body govuk-!-margin-bottom-1"> '
        'Firstname Lastname<br/> '
        '123 Example Street<br/> '
        '</p>'
    )
    assert uploads[1].select_one('a.file-list-filename-large')['href'] == (
        '/services/{}/notification/letter_id_1'.format(SERVICE_ONE_ID)
    )


def test_get_upload_letter(client_request):
    page = client_request.get('main.upload_letter', service_id=SERVICE_ONE_ID)

    assert page.find('h1').text == 'Upload a letter'
    assert page.find('input', class_='file-upload-field')
    assert page.select('button[type=submit]')
    assert normalize_spaces(page.find('label', class_='file-upload-button').text) == 'Choose file'


def test_post_upload_letter_redirects_for_valid_file(
    mocker,
    active_user_with_permissions,
    service_one,
    client_request,
    fake_uuid,
):
    mocker.patch('uuid.uuid4', return_value=fake_uuid)
    antivirus_mock = mocker.patch('app.main.views.uploads.antivirus_client.scan', return_value=True)
    mocker.patch(
        'app.main.views.uploads.sanitise_letter',
        return_value=Mock(
            content='The sanitised content',
            json=lambda: {'file': 'VGhlIHNhbml0aXNlZCBjb250ZW50', 'recipient_address': 'The Queen'}
        )
    )
    mock_s3 = mocker.patch('app.main.views.uploads.upload_letter_to_s3')
    mocker.patch('app.main.views.uploads.get_letter_metadata', return_value={
        'filename': 'tests/test_pdf_files/one_page_pdf.pdf',
        'page_count': '1',
        'status': 'valid',
        'recipient': 'The Queen'
    })
    mocker.patch('app.main.views.uploads.service_api_client.get_precompiled_template')

    service_one['restricted'] = False
    client_request.login(active_user_with_permissions, service=service_one)

    with open('tests/test_pdf_files/one_page_pdf.pdf', 'rb') as file:
        page = client_request.post(
            'main.upload_letter',
            service_id=SERVICE_ONE_ID,
            _data={'file': file},
            _follow_redirects=True,
        )
    assert antivirus_mock.called

    mock_s3.assert_called_once_with(
        b'The sanitised content',
        file_location='service-{}/{}.pdf'.format(SERVICE_ONE_ID, fake_uuid),
        status='valid',
        page_count=1,
        filename='tests/test_pdf_files/one_page_pdf.pdf',
        recipient='The Queen',
    )

    assert 'The Queen' in page.find('div', class_='js-stick-at-bottom-when-scrolling').text
    assert page.find('h1').text == 'tests/test_pdf_files/one_page_pdf.pdf'
    assert not page.find(id='validation-error-message')

    assert page.find('input', {'type': 'hidden', 'name': 'file_id', 'value': fake_uuid})
    assert normalize_spaces(page.select('main button[type=submit]')[0].text) == 'Send 1 letter'


def test_post_upload_letter_shows_letter_preview_for_valid_file(
    mocker,
    active_user_with_permissions,
    service_one,
    client_request,
    fake_uuid,
):
    letter_template = {'template_type': 'letter',
                       'reply_to_text': '',
                       'postage': 'second',
                       'subject': 'hi',
                       'content': 'my letter'}

    mocker.patch('uuid.uuid4', return_value=fake_uuid)
    mocker.patch('app.main.views.uploads.antivirus_client.scan', return_value=True)
    mocker.patch(
        'app.main.views.uploads.sanitise_letter',
        return_value=Mock(
            content='The sanitised content',
            json=lambda: {'file': 'VGhlIHNhbml0aXNlZCBjb250ZW50', 'recipient_address': 'The Queen'}
        )
    )
    mocker.patch('app.main.views.uploads.upload_letter_to_s3')
    mocker.patch('app.main.views.uploads.pdf_page_count', return_value=3)
    mocker.patch('app.main.views.uploads.get_letter_metadata', return_value={
        'filename': 'tests/test_pdf_files/one_page_pdf.pdf',
        'page_count': '3',
        'status': 'valid',
        'recipient': 'The Queen'
    })
    mocker.patch('app.main.views.uploads.service_api_client.get_precompiled_template', return_value=letter_template)

    service_one['restricted'] = False
    client_request.login(active_user_with_permissions, service=service_one)

    with open('tests/test_pdf_files/one_page_pdf.pdf', 'rb') as file:
        page = client_request.post(
            'main.upload_letter',
            service_id=SERVICE_ONE_ID,
            _data={'file': file},
            _follow_redirects=True,
        )

    assert page.find('h1').text == 'tests/test_pdf_files/one_page_pdf.pdf'
    assert len(page.select('.letter-postage')) == 0
    # Check postage radios exists and second class is checked by default
    assert page.find('input', id="postage-0", value="first")
    assert page.find('input', id="postage-1", value="second").has_attr('checked')

    letter_images = page.select('main img')
    assert len(letter_images) == 3

    for page_no, img in enumerate(letter_images, start=1):
        assert img['src'] == url_for(
            '.view_letter_upload_as_preview',
            service_id=SERVICE_ONE_ID,
            file_id=fake_uuid,
            page=page_no)


def test_post_upload_letter_shows_error_when_file_is_not_a_pdf(client_request):
    with open('tests/non_spreadsheet_files/actually_a_png.csv', 'rb') as file:
        page = client_request.post(
            'main.upload_letter',
            service_id=SERVICE_ONE_ID,
            _data={'file': file},
            _expected_status=200
        )
    assert page.find('h1').text == 'Wrong file type'
    assert page.find('div', class_='banner-dangerous').find('p').text == 'Save your letter as a PDF and try again.'
    assert normalize_spaces(page.find('label', class_='file-upload-button').text) == 'Upload your file again'


def test_post_upload_letter_shows_error_when_no_file_uploaded(client_request):
    page = client_request.post(
        'main.upload_letter',
        service_id=SERVICE_ONE_ID,
        _data={'file': ''},
        _expected_status=200
    )
    assert page.find('div', class_='banner-dangerous').find('h1').text == 'You need to choose a file to upload'
    assert normalize_spaces(page.find('label', class_='file-upload-button').text) == 'Upload your file again'


def test_post_upload_letter_shows_error_when_file_contains_virus(mocker, client_request):
    mocker.patch('app.main.views.uploads.antivirus_client.scan', return_value=False)

    with open('tests/test_pdf_files/one_page_pdf.pdf', 'rb') as file:
        page = client_request.post(
            'main.upload_letter',
            service_id=SERVICE_ONE_ID,
            _data={'file': file},
            _expected_status=400
        )
    assert page.find('div', class_='banner-dangerous').find('h1').text == 'Your file contains a virus'
    assert normalize_spaces(page.find('label', class_='file-upload-button').text) == 'Upload your file again'


def test_post_choose_upload_file_when_file_is_too_big(mocker, client_request):
    mocker.patch('app.main.views.uploads.antivirus_client.scan', return_value=True)

    with open('tests/test_pdf_files/big.pdf', 'rb') as file:
        page = client_request.post(
            'main.upload_letter',
            service_id=SERVICE_ONE_ID,
            _data={'file': file},
            _expected_status=400
        )
    assert page.find('div', class_='banner-dangerous').find('h1').text == 'Your file is too big'
    assert page.find('div', class_='banner-dangerous').find('p').text == 'Files must be smaller than 2MB.'
    assert normalize_spaces(page.find('label', class_='file-upload-button').text) == 'Upload your file again'


def test_post_choose_upload_file_when_file_is_malformed(mocker, client_request):
    mocker.patch('app.main.views.uploads.antivirus_client.scan', return_value=True)

    with open('tests/test_pdf_files/no_eof_marker.pdf', 'rb') as file:
        page = client_request.post(
            'main.upload_letter',
            service_id=SERVICE_ONE_ID,
            _data={'file': file},
            _expected_status=400
        )
    assert page.find('div', class_='banner-dangerous').find('h1').text == "There’s a problem with your file"
    assert page.find(
        'div', class_='banner-dangerous'
    ).find('p').text == 'Notify cannot read this PDF.Save a new copy of your file and try again.'
    assert normalize_spaces(page.find('label', class_='file-upload-button').text) == 'Upload your file again'


def test_post_upload_letter_with_invalid_file(mocker, client_request, fake_uuid):
    mocker.patch('uuid.uuid4', return_value=fake_uuid)
    mocker.patch('app.main.views.uploads.antivirus_client.scan', return_value=True)
    mock_s3 = mocker.patch('app.main.views.uploads.upload_letter_to_s3')

    mock_sanitise_response = Mock()
    mock_sanitise_response.raise_for_status.side_effect = RequestException(response=Mock(status_code=400))
    mock_sanitise_response.json = lambda: {
        "message": "content-outside-printable-area",
        "invalid_pages": [1]
    }
    mocker.patch('app.main.views.uploads.sanitise_letter', return_value=mock_sanitise_response)
    mocker.patch('app.main.views.uploads.service_api_client.get_precompiled_template')
    mocker.patch('app.main.views.uploads.get_letter_metadata', return_value={
        'filename': 'tests/test_pdf_files/one_page_pdf.pdf', 'page_count': '1', 'status': 'invalid',
        'message': 'content-outside-printable-area', 'invalid_pages': '[1]'})

    with open('tests/test_pdf_files/one_page_pdf.pdf', 'rb') as file:
        file_contents = file.read()
        file.seek(0)

        page = client_request.post(
            'main.upload_letter',
            service_id=SERVICE_ONE_ID,
            _data={'file': file},
            _follow_redirects=True
        )

        mock_s3.assert_called_once_with(
            file_contents,
            file_location='service-{}/{}.pdf'.format(SERVICE_ONE_ID, fake_uuid),
            status='invalid',
            page_count=1,
            filename='tests/test_pdf_files/one_page_pdf.pdf',
            invalid_pages=[1],
            message='content-outside-printable-area'
        )

    assert page.find('div', class_='banner-dangerous').find('h1', {"data-error-type": 'content-outside-printable-area'})
    assert not page.find('button', {'class': 'button', 'type': 'submit'})


def test_post_upload_letter_shows_letter_preview_for_invalid_file(mocker, client_request, fake_uuid):
    letter_template = {'template_type': 'letter',
                       'reply_to_text': '',
                       'postage': 'first',
                       'subject': 'hi',
                       'content': 'my letter'}

    mocker.patch('uuid.uuid4', return_value=fake_uuid)
    mocker.patch('app.main.views.uploads.antivirus_client.scan', return_value=True)
    mocker.patch('app.main.views.uploads.upload_letter_to_s3')
    mock_sanitise_response = Mock()
    mock_sanitise_response.raise_for_status.side_effect = RequestException(response=Mock(status_code=400))
    mock_sanitise_response.json = lambda: {"message": "template preview error", "recipient_address": "The Queen"}
    mocker.patch('app.main.views.uploads.sanitise_letter', return_value=mock_sanitise_response)
    mocker.patch('app.main.views.uploads.service_api_client.get_precompiled_template', return_value=letter_template)
    mocker.patch('app.main.views.uploads.get_letter_metadata', return_value={
        'filename': 'tests/test_pdf_files/one_page_pdf.pdf', 'page_count': '1', 'status': 'invalid',
        'message': 'template-preview-error'})

    with open('tests/test_pdf_files/one_page_pdf.pdf', 'rb') as file:
        page = client_request.post(
            'main.upload_letter',
            service_id=SERVICE_ONE_ID,
            _data={'file': file},
            _follow_redirects=True,
        )

    assert 'The Queen' not in page.text
    assert len(page.select('.letter-postage')) == 0

    assert page.find("a", {"class": "govuk-back-link"})["href"] == "/services/{}/upload-letter".format(SERVICE_ONE_ID)
    assert page.find("label", {"class": "file-upload-button"})

    letter_images = page.select('main img')
    assert len(letter_images) == 1
    assert letter_images[0]['src'] == url_for(
        '.view_letter_upload_as_preview',
        service_id=SERVICE_ONE_ID,
        file_id=fake_uuid,
        page=1
    )


def test_post_upload_letter_does_not_upload_to_s3_if_template_preview_raises_unknown_error(
    mocker,
    client_request,
    fake_uuid,
):
    mocker.patch('uuid.uuid4', return_value=fake_uuid)
    mocker.patch('app.main.views.uploads.antivirus_client.scan', return_value=True)
    mock_s3 = mocker.patch('app.main.views.uploads.upload_letter_to_s3')

    mocker.patch('app.main.views.uploads.sanitise_letter', side_effect=RequestException())

    with pytest.raises(RequestException):
        with open('tests/test_pdf_files/one_page_pdf.pdf', 'rb') as file:
            client_request.post(
                'main.upload_letter',
                service_id=SERVICE_ONE_ID,
                _data={'file': file},
                _follow_redirects=True
            )

    assert not mock_s3.called


def test_uploaded_letter_preview(
    mocker,
    active_user_with_permissions,
    service_one,
    client_request,
    fake_uuid,
):
    mocker.patch('app.main.views.uploads.service_api_client')
    recipient = 'Bugs Bunny\n123 Big Hole\rLooney Town'
    mocker.patch('app.main.views.uploads.get_letter_metadata', return_value={
        'filename': 'my_letter.pdf', 'page_count': '1', 'status': 'valid',
        'recipient': urllib.parse.quote(recipient)
        })

    service_one['restricted'] = False
    client_request.login(active_user_with_permissions, service=service_one)

    page = client_request.get(
        'main.uploaded_letter_preview',
        service_id=SERVICE_ONE_ID,
        file_id=fake_uuid,
        original_filename='my_letter.pdf',
        page_count=1,
        status='valid',
        error={}
    )

    assert page.find('h1').text == 'my_letter.pdf'
    assert page.find('div', class_='letter-sent')
    assert not page.find("label", {"class": "file-upload-button"})
    assert page.find('button', {'class': 'govuk-button', 'type': 'submit'})


def test_uploaded_letter_preview_does_not_show_send_button_if_service_in_trial_mode(
    mocker,
    client_request,
    fake_uuid,
):
    mocker.patch('app.main.views.uploads.service_api_client')
    mocker.patch('app.main.views.uploads.get_letter_metadata', return_value={
        'filename': 'my_letter.pdf', 'page_count': '1', 'status': 'valid', 'recipient': 'The Queen'})

    # client_request uses service_one, which is in trial mode
    page = client_request.get(
        'main.uploaded_letter_preview',
        service_id=SERVICE_ONE_ID,
        file_id=fake_uuid,
        original_filename='my_letter.pdf',
        page_count=1,
        status='valid',
        error={},
        _test_page_title=False,
    )

    assert normalize_spaces(page.find('h1').text) == 'You cannot send this letter'
    assert page.find('div', class_='letter-sent')
    assert normalize_spaces(
        page.select_one('.js-stick-at-bottom-when-scrolling p').text
    ) == (
        'Recipient: The Queen'
    )
    assert not page.find('form')
    assert len(page.select('main button[type=submit]')) == 0


@pytest.mark.parametrize('invalid_pages, page_requested, overlay_expected', (
    ('[1, 2]', 1, True),
    ('[1, 2]', 2, True),
    ('[1, 2]', 3, False),
    ('[]', 1, False),
))
def test_uploaded_letter_preview_image_shows_overlay_when_content_outside_printable_area_on_a_page(
    mocker,
    logged_in_client,
    mock_get_service,
    fake_uuid,
    invalid_pages,
    page_requested,
    overlay_expected,
):
    mocker.patch(
        'app.main.views.uploads.get_letter_pdf_and_metadata',
        return_value=('pdf_file', {
            'message': 'content-outside-printable-area',
            'invalid_pages': invalid_pages,
        })
    )
    template_preview_mock_valid = mocker.patch(
        'app.main.views.uploads.TemplatePreview.from_valid_pdf_file',
        return_value=make_response('page.html', 200)
    )
    template_preview_mock_invalid = mocker.patch(
        'app.main.views.uploads.TemplatePreview.from_invalid_pdf_file',
        return_value=make_response('page.html', 200)
    )

    logged_in_client.get(
        url_for(
            'main.view_letter_upload_as_preview',
            file_id=fake_uuid,
            service_id=SERVICE_ONE_ID,
            page=page_requested,
        )
    )

    if overlay_expected:
        template_preview_mock_invalid.assert_called_once_with('pdf_file', page_requested)
        assert template_preview_mock_valid.called is False
    else:
        template_preview_mock_valid.assert_called_once_with('pdf_file', page_requested)
        assert template_preview_mock_invalid.called is False


@pytest.mark.parametrize(
    'metadata', [
        {'message': 'letter-not-a4-portrait-oriented'},
        {'message': 'letter-too-long'},
        {},
    ]
)
def test_uploaded_letter_preview_image_does_not_show_overlay_if_no_content_outside_printable_area(
    mocker,
    logged_in_client,
    mock_get_service,
    metadata,
    fake_uuid,
):
    mocker.patch(
        'app.main.views.uploads.get_letter_pdf_and_metadata',
        return_value=('pdf_file', metadata)
    )
    template_preview_mock = mocker.patch(
        'app.main.views.uploads.TemplatePreview.from_valid_pdf_file',
        return_value=make_response('page.html', 200))

    logged_in_client.get(
        url_for('main.view_letter_upload_as_preview', file_id=fake_uuid, service_id=SERVICE_ONE_ID, page=1)
    )

    template_preview_mock.assert_called_once_with('pdf_file', 1)


def test_uploaded_letter_preview_image_400s_for_bad_page_type(
    client_request,
    fake_uuid,
):
    client_request.get(
        'main.view_letter_upload_as_preview',
        file_id=fake_uuid,
        service_id=SERVICE_ONE_ID,
        page='foo',
        _test_page_title=False,
        _expected_status=400,
    )


def test_send_uploaded_letter_sends_letter_and_redirects_to_notification_page(mocker, service_one, client_request):
    metadata = {'filename': 'my_file.pdf', 'page_count': '1', 'status': 'valid', 'recipient': 'address'}

    mocker.patch('app.main.views.uploads.get_letter_pdf_and_metadata', return_value=('file', metadata))
    mock_send = mocker.patch('app.main.views.uploads.notification_api_client.send_precompiled_letter')
    mocker.patch('app.main.views.uploads.get_letter_metadata', return_value=metadata)

    service_one['permissions'] = ['letter', 'upload_letters']
    file_id = 'abcd-1234'

    client_request.post(
        'main.send_uploaded_letter',
        service_id=SERVICE_ONE_ID,
        _data={'filename': 'my_file.pdf', 'file_id': file_id, 'postage': 'first'},
        _expected_redirect=url_for(
            'main.view_notification',
            service_id=SERVICE_ONE_ID,
            notification_id=file_id,
            _external=True
        )
    )
    mock_send.assert_called_once_with(SERVICE_ONE_ID, 'my_file.pdf', file_id, 'first', 'address')


@pytest.mark.parametrize('permissions', [
    ['email'],
    ['letter'],
    ['upload_letters'],
])
def test_send_uploaded_letter_when_service_does_not_have_correct_permissions(
    mocker,
    service_one,
    client_request,
    permissions,
):
    mocker.patch('app.main.views.uploads.get_letter_pdf_and_metadata', return_value=('file', {'status': 'valid'}))
    mock_send = mocker.patch('app.main.views.uploads.notification_api_client.send_precompiled_letter')

    service_one['permissions'] = permissions
    file_id = 'abcd-1234'

    client_request.post(
        'main.send_uploaded_letter',
        service_id=SERVICE_ONE_ID,
        _data={'filename': 'my_file.pdf', 'file_id': file_id, 'postage': 'first'},
        _expected_status=403
    )
    assert not mock_send.called


def test_send_uploaded_letter_when_metadata_states_pdf_is_invalid(mocker, service_one, client_request):
    mock_send = mocker.patch('app.main.views.uploads.notification_api_client.send_precompiled_letter')
    mocker.patch(
        'app.main.views.uploads.get_letter_metadata',
        return_value={'filename': 'my_file.pdf', 'page_count': '3', 'status': 'invalid',
                      'message': 'error', 'invalid_pages': '[1]'}
    )

    service_one['permissions'] = ['letter', 'upload_letters']
    file_id = 'abcd-1234'

    client_request.post(
        'main.send_uploaded_letter',
        service_id=SERVICE_ONE_ID,
        _data={'filename': 'my_file.pdf', 'file_id': file_id},
        _expected_status=403
    )
    assert not mock_send.called


@pytest.mark.parametrize('original_address,expected_address', [
    ('The Queen, Buckingham Palace, SW1 1AA', 'The Queen, Buckingham Palace, SW1 1AA'),
    ('The Queen Buckingham Palace SW1 1AA', 'The Queen Buckingham Palace SW1 1AA'),
    ('The Queen,\nBuckingham Palace,\r\nSW1 1AA', 'The Queen, Buckingham Palace, SW1 1AA'),
    ('The Queen   ,,\nBuckingham Palace,\rSW1 1AA,', 'The Queen, Buckingham Palace, SW1 1AA'),
    ('  The Queen\n Buckingham Palace\n SW1 1AA', 'The Queen, Buckingham Palace, SW1 1AA'),
    ('', ''),
])
def test_format_recipient(original_address, expected_address):
    assert format_recipient(urllib.parse.quote(original_address)) == expected_address


@pytest.mark.parametrize('user', (
    create_active_caseworking_user(),
    create_active_user_with_permissions(),
))
@freeze_time("2012-12-12 12:12")
def test_uploads_page_shows_scheduled_jobs(
    mocker,
    client_request,
    mock_get_no_uploads,
    mock_get_jobs,
    user,
):
    client_request.login(user)
    page = client_request.get('main.uploads', service_id=SERVICE_ONE_ID)

    assert [
        normalize_spaces(row.text) for row in page.select('tr')
    ] == [
        (
            'File Status'
        ),
        (
            'send_me_later.csv '
            'Sending 1 January 2016 at 11:09am '
            '1 text message waiting to send'
        ),
        (
            'even_later.csv '
            'Sending 1 January 2016 at 11:09pm '
            '1 text message waiting to send'
        ),
    ]
    assert not page.select('.table-empty-message')


def test_get_uploads_shows_pagination(
    client_request,
    active_user_with_permissions,
    mock_get_jobs,
    mock_get_uploads,
):
    page = client_request.get('main.uploads', service_id=SERVICE_ONE_ID)

    assert normalize_spaces(page.select_one('.next-page').text) == (
        'Next page '
        'page 2'
    )
    assert normalize_spaces(page.select_one('.previous-page').text) == (
        'Previous page '
        'page 0'
    )
