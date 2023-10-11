import shippo  # type: ignore[import]

from discord_blue.config import config


if __name__ == "__main__":
    shippo.config.api_key = config.shippo.api_key

    address_from = {
        "name": "Shippo Team",
        "street1": "965 Mission St",
        "street2": "Unit 480",
        "city": "San Francisco",
        "state": "CA",
        "zip": "94103",
        "country": "US",
        "phone": "+1 555 341 9393",
    }

    address1 = shippo.Address.create(
        name="Mr Hippo",
        street1="215 Clayton St.",
        city="San Francisco",
        state="CA",
        zip="94117",
        country="US",
        phone="+1 555 341 9393",
        company="Shippo",
        metadata="Customer ID 123456",
    )

    parcel = shippo.Parcel.create(
        length="5",
        width="5",
        height="5",
        distance_unit="in",
        weight="2",
        mass_unit="lb",
    )

    shipment = shippo.Shipment.create(
        address_from=address_from,
        address_to=address1,
        parcels=[parcel],
        asynchronous=False,
    )

    rate = shipment.rates[0]
    transaction = shippo.Transaction.create(
        rate=rate.object_id,
        asynchronous=False,
        label_file_type="PDF_4x6",
    )
    if transaction.status == "SUCCESS":
        print("Purchased label with tracking number %s" % transaction.tracking_number)
        print("The label can be downloaded at %s" % transaction.label_url)
    else:
        print("Failed purchasing the label due to:")
        for message in transaction.messages:
            print("- %s" % message["text"])
